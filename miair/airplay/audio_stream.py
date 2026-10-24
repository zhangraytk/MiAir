"""AirPlay 音频 HTTP 流输出

将解码后的 PCM 音频数据转换为 HTTP 音频流，供小爱音箱播放。
支持两种输出格式:
- MP3: 兼容性好，适用于 L05B/L05C/LX06/L16A 等不支持 WAV 的音箱
- WAV: 零编码延迟，直接输出 PCM，适用于大多数音箱
"""

import asyncio
import logging
import queue
import struct
import subprocess
import threading
import time

from aiohttp import web

log = logging.getLogger("miair")

# 每个 ALAC 包约 8ms (352 samples @ 44100Hz)
# 100 个包 ≈ 800ms 的缓冲上限，兼顾低延迟与抗 WiFi 抖动
_QUEUE_MAXSIZE = 100


class AudioStreamServer:
    """HTTP 音频流服务器

    接收 PCM 音频数据，通过 HTTP 提供给小爱音箱播放。
    根据音箱型号选择 MP3 (ffmpeg 转码) 或 WAV (直接输出) 格式。
    支持纳秒级单调时钟观测、连续 PCM 时间轴毫秒级采样偏移与预缓冲补偿。
    """

    def __init__(
        self,
        hostname: str,
        port: int = 0,
        audio_format: str = "wav",
        latency_offset_ms: int = 0,
        prebuffer_ms: int = 0,
        sync_debug: bool = False,
    ):
        self.hostname = hostname
        self.port = port
        self._audio_format = audio_format  # "mp3" or "wav"
        self.latency_offset_ms = latency_offset_ms
        self.prebuffer_ms = prebuffer_ms
        self.sync_debug = sync_debug
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        # 音频数据队列 - 有界缓冲保证内存安全
        self._audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._sample_rate = 44100
        self._channels = 2
        self._sample_width = 2  # 16-bit
        self._active = False
        self._abort = False
        self._session_id = int(time.time())
        self._has_clients = False
        self._client_lock = threading.Lock()
        self._queue_bytes: int = 0

        # 时间观测体系 (单调时钟 time.monotonic)
        self._timing = {
            "session_start_time": 0.0,
            "pcm_first_data_time": 0.0,
            "http_stream_ready_time": 0.0,
            "speaker_http_connected_time": 0.0,
            "first_audio_sent_time": 0.0,
            "overflow_count": 0,
            "underflow_count": 0,
            "dropped_frames": 0,
            "stop_count": 0,
            "reconnect_count": 0,
        }

        self._setup_routes()

    def _setup_routes(self):
        if self._audio_format == "mp3":
            self._app.router.add_get("/airplay/stream.mp3", self._handle_stream_mp3)
        else:
            self._app.router.add_get("/airplay/stream.wav", self._handle_stream_wav)

    @property
    def stream_url(self) -> str:
        ext = "mp3" if self._audio_format == "mp3" else "wav"
        return f"http://{self.hostname}:{self.port}/airplay/stream.{ext}?sid={self._session_id}"

    async def start(self):
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]
        self._timing["http_stream_ready_time"] = time.monotonic()
        log.info(f"AirPlay 音频流服务器: http://{self.hostname}:{self.port} (格式: {self._audio_format})")

    async def stop(self):
        self._active = False
        try:
            self._audio_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._runner:
            await self._runner.cleanup()

    def set_audio_params(self, sample_rate: int, channels: int, sample_width: int = 2):
        self._sample_rate = sample_rate
        self._channels = channels
        self._sample_width = sample_width

    def start_streaming(self):
        self._active = True
        self._abort = False
        self._session_id = int(time.time())
        self._timing["session_start_time"] = time.monotonic()
        self._timing["pcm_first_data_time"] = 0.0
        self._timing["speaker_http_connected_time"] = 0.0
        self._timing["first_audio_sent_time"] = 0.0
        # 快速清空队列
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        self._queue_bytes = 0
        log.info(f"音频流: 开始接收 PCM 数据 (格式: {self._audio_format})")

    def stop_streaming(self):
        self._active = False
        self._abort = True
        self._timing["stop_count"] += 1
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        self._queue_bytes = 0
        try:
            self._audio_queue.put_nowait(None)
        except queue.Full:
            pass
        log.info("音频流: 停止接收 PCM 数据")

    def write_pcm(self, data: bytes):
        """写入 PCM 音频数据 — 非阻塞，队列满时批量丢弃旧数据腾出空间"""
        if not self._active:
            return
        now = time.monotonic()
        if not self._timing["pcm_first_data_time"]:
            self._timing["pcm_first_data_time"] = now
            if self.sync_debug:
                delta = now - self._timing["session_start_time"] if self._timing["session_start_time"] else 0.0
                log.info(f"[{self.hostname}:{self.port}] PCM 首次流入 FIFO/AudioStream, 耗时: {delta:.3f}s")

        try:
            self._audio_queue.put_nowait(data)
            self._queue_bytes += len(data)
        except queue.Full:
            self._timing["overflow_count"] += 1
            dropped_bytes = 0
            # 批量丢弃旧数据，一次性腾出足够空间
            target = max(1, _QUEUE_MAXSIZE // 4)
            try:
                for _ in range(target):
                    chunk = self._audio_queue.get_nowait()
                    if chunk:
                        dropped_bytes += len(chunk)
            except queue.Empty:
                pass
            frame_size = self._channels * self._sample_width
            self._timing["dropped_frames"] += dropped_bytes // frame_size
            self._queue_bytes = max(0, self._queue_bytes - dropped_bytes)
            try:
                self._audio_queue.put_nowait(data)
                self._queue_bytes += len(data)
            except queue.Full:
                pass

    def get_timing_stats(self) -> dict:
        """获取当前音频流的高精度单调时钟观测与缓冲状态"""
        now = time.monotonic()
        q_size = self._audio_queue.qsize()
        frame_size = max(1, self._channels * self._sample_width)
        bytes_in_q = max(0, self._queue_bytes)
        approx_buffer_ms = round((bytes_in_q / frame_size) / (self._sample_rate / 1000.0), 1)
        sess_start = self._timing["session_start_time"]
        return {
            "session_active": self._active,
            "has_client": self._has_clients,
            "session_start_time": sess_start,
            "pcm_first_data_time": self._timing["pcm_first_data_time"],
            "http_stream_ready_time": self._timing["http_stream_ready_time"],
            "speaker_http_connected_time": self._timing["speaker_http_connected_time"],
            "first_audio_sent_time": self._timing["first_audio_sent_time"],
            "elapsed_since_session_start_sec": round(now - sess_start, 3) if sess_start else 0.0,
            "audio_queue_depth": q_size,
            "buffer_duration_ms": approx_buffer_ms,
            "overflow_count": self._timing["overflow_count"],
            "underflow_count": self._timing["underflow_count"],
            "dropped_frames": self._timing["dropped_frames"],
            "stop_count": self._timing["stop_count"],
            "reconnect_count": self._timing["reconnect_count"],
            "latency_offset_ms": self.latency_offset_ms,
            "prebuffer_ms": self.prebuffer_ms,
        }

    # ============================================================
    # WAV 模式 — 直接输出 PCM，零编码延迟
    # ============================================================

    def _reject_if_inactive(self) -> web.Response | None:
        """流已停止（TEARDOWN 后）返回 404 拒绝拉取，避免音箱把空响应当成 EOF
        反复重连该 URL 造成死循环（氛围灯闪烁、固件崩溃）。未停止返回 None。
        """
        if not self._active:
            log.info("AirPlay: 流已停止，拒绝拉取 (404)")
            return web.Response(status=404, headers={"Connection": "close"})
        return None

    def _build_wav_header(self, data_size: int = 0x7FFFFF00) -> bytes:
        byte_rate = self._sample_rate * self._channels * self._sample_width
        block_align = self._channels * self._sample_width
        bits_per_sample = self._sample_width * 8
        return struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', data_size + 36, b'WAVE',
            b'fmt ', 16, 1, self._channels,
            self._sample_rate, byte_rate, block_align, bits_per_sample,
            b'data', data_size,
        )

    async def _handle_stream_wav(self, request: web.Request) -> web.StreamResponse:
        """WAV 模式: 直接输出 PCM 数据，零编码延迟

        使用专用写入线程从队列批量读取数据，通过 asyncio 事件写回 HTTP 响应，
        避免每个包都经过 asyncio.to_thread 的调度开销。
        """
        rejected = self._reject_if_inactive()
        if rejected is not None:
            return rejected

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/wav",
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
                "Connection": "close",
                "Transfer-Encoding": "chunked",
            },
        )
        await response.prepare(request)

        # 关闭 Nagle 算法，让小包立即发送而不等待合并
        transport = request.transport
        if transport is not None:
            sock = transport.get_extra_info('socket')
            if sock is not None:
                import socket as _sock
                sock.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_NODELAY, 1)

        with self._client_lock:
            self._has_clients = True
        self._abort = False  # 重置中断标志，允许续播

        now = time.monotonic()
        if self._timing["speaker_http_connected_time"] > 0:
            self._timing["reconnect_count"] += 1
        self._timing["speaker_http_connected_time"] = now
        if self.sync_debug:
            delta_sess = now - self._timing["session_start_time"] if self._timing["session_start_time"] else 0.0
            delta_pcm = now - self._timing["pcm_first_data_time"] if self._timing["pcm_first_data_time"] else 0.0
            log.info(f"[{self.hostname}:{self.port}] 音箱 HTTP 已连接 (距会话: {delta_sess:.3f}s, 距首PCM: {delta_pcm:.3f}s)")
        log.info("AirPlay: 音箱开始拉取 WAV 音频流 (零编码延迟)")

        # 计算连续 PCM 时间轴偏移补偿（对齐到 4 字节帧边界）
        frame_size = self._channels * self._sample_width  # 4 字节
        offset_ms = self.latency_offset_ms
        if offset_ms > 0:
            # 延后播放：在音频数据最开始填充静音帧
            delay_frames = int(round(self._sample_rate * offset_ms / 1000.0))
            initial_silence = b"\x00" * (delay_frames * frame_size)
            bytes_to_skip = 0
        elif offset_ms < 0:
            # 提前播放：跳过音频流最开始的一段采样
            initial_silence = b""
            bytes_to_skip = int(round(self._sample_rate * abs(offset_ms) / 1000.0)) * frame_size
        else:
            initial_silence = b""
            bytes_to_skip = 0

        prebuffer_bytes = int(round(self._sample_rate * max(0, self.prebuffer_ms) / 1000.0)) * frame_size

        # 使用 asyncio.Event 在写入线程和事件循环间通信
        loop = asyncio.get_event_loop()
        data_ready = asyncio.Event()
        pending_data: list[bytes] = []
        data_lock = threading.Lock()
        writer_done = False

        def _reader_thread():
            """专用线程：从队列批量读取 PCM 并通知事件循环"""
            nonlocal writer_done
            empty_streak = 0
            prebuffer_accumulated = 0
            is_prebuffering = (prebuffer_bytes > 0)
            prebuffer_start = time.monotonic()
            try:
                while self._active and not self._abort:
                    try:
                        chunk = self._audio_queue.get(timeout=0.02)
                        if chunk is None:
                            break
                        # 本地批量收集，避免每个 chunk 都加锁
                        local_batch = [chunk]
                        for _ in range(31):
                            try:
                                extra = self._audio_queue.get_nowait()
                                if extra is None:
                                    break
                                local_batch.append(extra)
                            except queue.Empty:
                                break
                        with data_lock:
                            pending_data.extend(local_batch)
                        self._queue_bytes = max(0, self._queue_bytes - sum(len(c) for c in local_batch))

                        if is_prebuffering:
                            prebuffer_accumulated += sum(len(c) for c in local_batch)
                            if prebuffer_accumulated >= prebuffer_bytes or (time.monotonic() - prebuffer_start > 0.8):
                                is_prebuffering = False
                                loop.call_soon_threadsafe(data_ready.set)
                        else:
                            loop.call_soon_threadsafe(data_ready.set)
                        empty_streak = 0
                    except queue.Empty:
                        empty_streak += 1
                        # 3 秒无数据写入静音保持连接
                        if empty_streak > 100:
                            self._timing["underflow_count"] += 1
                            silence = b'\x00' * (self._sample_rate * self._channels * self._sample_width // 50)
                            with data_lock:
                                pending_data.append(silence)
                            loop.call_soon_threadsafe(data_ready.set)
                        continue
            except Exception as e:
                log.error(f"WAV reader 异常: {e}")
            finally:
                writer_done = True
                loop.call_soon_threadsafe(data_ready.set)

        reader = threading.Thread(target=_reader_thread, daemon=True)
        reader.start()

        try:
            # 发送 WAV 头
            await response.write(self._build_wav_header())

            # 若配置了正延迟补偿，先发送连续且对齐的静音填充
            if initial_silence:
                await response.write(initial_silence)

            while not writer_done:
                await data_ready.wait()
                data_ready.clear()
                with data_lock:
                    chunks = pending_data
                    pending_data = []
                if chunks:
                    chunk_bytes = b"".join(chunks)
                    if bytes_to_skip > 0:
                        if len(chunk_bytes) <= bytes_to_skip:
                            bytes_to_skip -= len(chunk_bytes)
                            continue
                        else:
                            chunk_bytes = chunk_bytes[bytes_to_skip:]
                            bytes_to_skip = 0

                    if self._timing["first_audio_sent_time"] == 0:
                        self._timing["first_audio_sent_time"] = time.monotonic()
                        if self.sync_debug:
                            delta_conn = self._timing["first_audio_sent_time"] - self._timing["speaker_http_connected_time"]
                            log.info(f"[{self.hostname}:{self.port}] 首批音频数据已发送至音箱 (从音箱连接到首包耗时: {delta_conn:.3f}s)")

                    await response.write(chunk_bytes)
        except (ConnectionResetError, BrokenPipeError):
            log.info("AirPlay: 音箱断开 WAV 音频流连接")
        except Exception as e:
            pass
        finally:
            self._abort = True  # 通知 reader 线程退出
            with self._client_lock:
                self._has_clients = False

        try:
            await response.write_eof()
        except Exception:
            pass
        log.info("AirPlay: WAV 音频流结束")
        return response

    # ============================================================
    # MP3 模式 — ffmpeg 实时转码 (用于不支持 WAV 的音箱)
    # ============================================================

    async def _handle_stream_mp3(self, request: web.Request) -> web.StreamResponse:
        rejected = self._reject_if_inactive()
        if rejected is not None:
            return rejected

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
                "Connection": "close",
            },
        )
        await response.prepare(request)

        # 关闭 Nagle 算法
        transport = request.transport
        if transport is not None:
            sock = transport.get_extra_info('socket')
            if sock is not None:
                import socket as _sock
                sock.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_NODELAY, 1)

        with self._client_lock:
            self._has_clients = True
        self._abort = False  # 重置中断标志，允许续播

        log.info("AirPlay: 音箱开始拉取 MP3 音频流")

        import os
        ffmpeg_bin = "ffmpeg"
        if os.path.exists("ffmpeg.exe"):
            ffmpeg_bin = os.path.abspath("ffmpeg.exe")
        elif os.path.exists("bin/ffmpeg.exe"):
            ffmpeg_bin = os.path.abspath("bin/ffmpeg.exe")

        ffmpeg_cmd = [
            ffmpeg_bin,
            "-loglevel", "error",
            "-fflags", "+nobuffer+flush_packets",
            "-flags", "low_delay",
            "-f", "s16le",
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            "-i", "pipe:0",
            "-acodec", "libmp3lame",
            "-compression_level", "0",  # 最快编码速度
            "-ab", "128k",
            "-ac", "2",
            "-ar", "44100",
            "-flush_packets", "1",
            "-id3v2_version", "0",
            "-f", "mp3",
            "pipe:1"
        ]

        try:
            proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            log.error("ffmpeg 未找到，无法转码音频流")
            with self._client_lock:
                self._has_clients = False
            await response.write_eof()
            return response

        def drain_stderr():
            try:
                while True:
                    line = proc.stderr.readline()
                    if not line:
                        break
                    line_str = line.decode('utf-8', errors='ignore').strip()
                    if line_str and 'rror' in line_str:
                        pass
            except Exception:
                pass

        threading.Thread(target=drain_stderr, daemon=True).start()

        def feed_ffmpeg():
            empty_streak = 0
            try:
                while True:
                    try:
                        chunk = self._audio_queue.get(timeout=0.05)
                        if chunk is None:
                            break
                        # 批量收集数据后一次性写入，减少 write 系统调用次数
                        chunks = [chunk]
                        for _ in range(15):
                            try:
                                extra = self._audio_queue.get_nowait()
                                if extra is None:
                                    proc.stdin.close()
                                    return
                                chunks.append(extra)
                            except queue.Empty:
                                break
                        proc.stdin.write(b"".join(chunks))
                        empty_streak = 0
                    except queue.Empty:
                        empty_streak += 1
                        if empty_streak > 40:
                            silence = b'\x00' * (self._sample_rate * self._channels * self._sample_width // 50)
                            try:
                                proc.stdin.write(silence)
                            except Exception:
                                break
                        continue
                    except (BrokenPipeError, OSError):
                        break
            except Exception as e:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        threading.Thread(target=feed_ffmpeg, daemon=True).start()

        # 从 ffmpeg stdout 读取并写给客户端
        try:
            while self._active and not self._abort:
                audio_data = await asyncio.to_thread(proc.stdout.read, 8192)
                if not audio_data or self._abort:
                    break
                await response.write(audio_data)
        except (ConnectionResetError, BrokenPipeError):
            log.info("AirPlay: 音箱断开 MP3 音频流连接")
        except Exception as e:
            pass
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

        with self._client_lock:
            self._has_clients = False

        try:
            await response.write_eof()
        except Exception:
            pass
        log.info("AirPlay: MP3 音频流结束")
        return response
