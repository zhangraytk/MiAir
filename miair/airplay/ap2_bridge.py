"""AirPlay 2 桥接服务与音频管道

架构说明：
  iOS / macOS 发送端
        ↓ (AirPlay 2 协议 / PTP 时钟同步)
  Shairport Sync (C 引擎 + NQPTP)
        ↓ (PCM S16LE 44100Hz 2ch 写入 Unix FIFO)
  FIFOAudioBridge (Python 管道消费线程)
        ↓ (调用 write_pcm 写入有界队列)
  AudioStreamServer (复用 MiAir 现有 HTTP 流服务)
        ↓ (HTTP WAV / MP3 流)
  小米小爱音箱硬件 (SpeakerController.play_url)
"""

import asyncio
import logging
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

from miair.airplay.audio_stream import AudioStreamServer

log = logging.getLogger("miair")


def sanitize_filename(name: str) -> str:
    """清理文件名字符"""
    return re.sub(r"[^\w\-.]", "_", name)


class ShairportSyncConfig:
    """生成 Shairport Sync 配置文件"""

    @staticmethod
    def generate(
        config_path: str,
        device_name: str,
        rtsp_port: int,
        fifo_path: str,
        notify_port: int,
        python_bin: str = sys.executable,
        mdns_backend: str = "avahi",
    ) -> str:
        """生成并写入 shairport-sync.conf"""
        # 构造通知命令
        notify_script = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "ap2_notify.py")
        )
        cmd_start = f'"{python_bin}" "{notify_script}" {notify_port} play_begin'
        cmd_stop = f'"{python_bin}" "{notify_script}" {notify_port} play_end'
        cmd_vol = f'"{python_bin}" "{notify_script}" {notify_port} volume '

        content = f"""// MiAir 自动生成的 Shairport Sync AirPlay 2 配置文件
general = {{
    name = "{device_name}";
    output_backend = "pipe";
    port = {rtsp_port};
    run_this_when_volume_is_set = "{cmd_vol}";
    mdns_backend = "{mdns_backend}";
}};

sessioncontrol = {{
    run_this_before_play_begins = "{cmd_start}";
    run_this_after_play_ends = "{cmd_stop}";
    wait_for_completion = "no";
    allow_session_interruption = "yes";
}};

pipe = {{
    name = "{fifo_path}";
}};

metadata = {{
    enabled = "no";
}};
"""
        os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
        return config_path


class FIFOAudioBridge:
    """Unix 命名管道 (FIFO) 音频桥接器

    特性：
    - 使用 O_RDWR 打开 FIFO，避免写端关闭时读端直接触发 EOF
    - 基于 select.select 进行非阻塞轮询，支持可配置的超时与安全退出
    - 有界缓冲推送至 AudioStreamServer，防止内存无限累积
    - 兼具显式事件钩子与音频数据流活动自动感应 (双保险)
    """

    DEFAULT_CHUNK_SIZE = 4096  # 1024 samples @ 16-bit 2ch ≈ 23.2ms
    DEFAULT_SILENCE_TIMEOUT = 3.5  # 超过 3.5 秒无音频数据判定为流中断/暂停

    def __init__(
        self,
        fifo_path: str,
        audio_stream_server: AudioStreamServer,
        on_active_change: Callable[[bool], None] | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        silence_timeout: float = DEFAULT_SILENCE_TIMEOUT,
    ):
        self.fifo_path = fifo_path
        self.audio_stream_server = audio_stream_server
        self.on_active_change = on_active_change
        self.chunk_size = chunk_size
        self.silence_timeout = silence_timeout

        self._running = False
        self._thread: threading.Thread | None = None
        self._fifo_fd: int | None = None
        self._is_streaming = False
        self._last_data_time = 0.0
        self._total_bytes_read = 0

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def total_bytes_read(self) -> int:
        return self._total_bytes_read

    def setup_fifo(self):
        """创建 FIFO 文件"""
        if os.path.exists(self.fifo_path):
            try:
                os.remove(self.fifo_path)
            except OSError as e:
                log.warning(f"[AirPlay 2] 清理旧 FIFO 失败 {self.fifo_path}: {e}")

        os.makedirs(os.path.dirname(os.path.abspath(self.fifo_path)), exist_ok=True)
        try:
            os.mkfifo(self.fifo_path, 0o666)
            log.info(f"[AirPlay 2] 已创建音频 FIFO: {self.fifo_path}")
        except OSError as e:
            log.error(f"[AirPlay 2] 创建 FIFO 失败 {self.fifo_path}: {e}")
            raise

    def start(self):
        """启动 FIFO 读取线程"""
        if self._running:
            return
        self.setup_fifo()

        # 使用 O_RDWR 模式打开，持久保持文件句柄活跃
        try:
            self._fifo_fd = os.open(self.fifo_path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            log.error(f"[AirPlay 2] 打开 FIFO 失败 {self.fifo_path}: {e}")
            raise

        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, name="AP2-FIFO-Reader", daemon=True
        )
        self._thread.start()
        log.info(f"[AirPlay 2] 音频管道监听已启动: {self.fifo_path}")

    def stop(self):
        """停止 FIFO 读取并清理"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

        if self._fifo_fd is not None:
            try:
                os.close(self._fifo_fd)
            except OSError:
                pass
            self._fifo_fd = None

        if os.path.exists(self.fifo_path):
            try:
                os.remove(self.fifo_path)
            except OSError:
                pass

        if self._is_streaming:
            self._is_streaming = False
            if self.on_active_change:
                self.on_active_change(False)
        log.info(f"[AirPlay 2] 音频管道已停止: {self.fifo_path}")

    def set_explicit_streaming(self, active: bool):
        """由外部事件钩子显式通知流状态变化"""
        if active and not self._is_streaming:
            self._is_streaming = True
            self._last_data_time = time.time()
            if self.on_active_change:
                self.on_active_change(True)
        elif not active and self._is_streaming:
            self._is_streaming = False
            if self.on_active_change:
                self.on_active_change(False)

    def _read_loop(self):
        """核心轮询与数据消费循环"""
        while self._running:
            if self._fifo_fd is None:
                break

            try:
                # 使用 select 检查可读性，超时 0.2 秒
                r, _, _ = select.select([self._fifo_fd], [], [], 0.2)
                if not self._running:
                    break

                if r:
                    data = os.read(self._fifo_fd, self.chunk_size)
                    if data:
                        now = time.time()
                        self._last_data_time = now
                        self._total_bytes_read += len(data)

                        # 如果当前未在播放状态，自动感应流启动
                        if not self._is_streaming:
                            self._is_streaming = True
                            log.info(
                                f"[AirPlay 2] 侦测到音频流流入 ({len(data)} 字节)，标记活跃"
                            )
                            if self.on_active_change:
                                self.on_active_change(True)

                        # 写入 MiAir 音频流服务器
                        self.audio_stream_server.write_pcm(data)
                else:
                    # select 超时：检查静音/流中断
                    if self._is_streaming:
                        idle_time = time.time() - self._last_data_time
                        if idle_time > self.silence_timeout:
                            log.info(
                                f"[AirPlay 2] 管道无数据超过 {idle_time:.1f} 秒，判定为空闲暂停"
                            )
                            self._is_streaming = False
                            if self.on_active_change:
                                self.on_active_change(False)

            except (BlockingIOError, InterruptedError):
                continue
            except Exception as e:
                if self._running:
                    log.error(f"[AirPlay 2] FIFO 读取异常: {e}")
                break


class AirPlay2Server:
    """AirPlay 2 接收服务组件

    符合 AirPlayServer duck-typing 接口契约：
    - rtsp_port: int
    - is_playing: bool
    - client_name: str
    - on_play_start: Callable[[str], None]
    - on_play_stop: Callable[[], None]
    - on_volume_change: Callable[[float], None]
    - async def start()
    - async def stop()
    """

    def __init__(
        self,
        hostname: str,
        device_name: str,
        speaker_hardware: str = "",
        rtsp_port: int = 7000,
        temp_dir: str = "",
        config=None,
    ):
        self.hostname = hostname
        self.raw_device_name = device_name
        self.speaker_hardware = speaker_hardware
        self.rtsp_port = rtsp_port
        self.config = config

        # 设备广播名增加标识，避免与现有 AirPlay 1 重名冲突
        suffix = " [AP2]"
        if config and hasattr(config, "airplay2_name_suffix"):
            suffix = config.airplay2_name_suffix
        self.device_name = f"{device_name}{suffix}"

        # 临时工作目录
        self.work_dir = temp_dir or tempfile.gettempdir()
        safe_name = sanitize_filename(device_name)
        self.fifo_path = os.path.join(self.work_dir, f"miair_ap2_{safe_name}.pcm")
        self.conf_path = os.path.join(self.work_dir, f"miair_sps_{safe_name}.conf")

        # 音频格式选择：依据硬件特性选择 WAV 或 MP3
        from miair.speaker import Speaker

        temp_spk = Speaker(hardware=speaker_hardware)
        audio_fmt = "mp3" if temp_spk.needs_audio_conversion() else "wav"

        # 复用 MiAir 现有 AudioStreamServer
        self.audio_stream_server = AudioStreamServer(
            hostname=self.hostname, port=0, audio_format=audio_fmt
        )
        self.audio_stream_server.set_audio_params(
            sample_rate=44100, channels=2, sample_width=2
        )

        # 状态及回调
        self.client_name = "AirPlay 2 Client"
        self._last_volume_db = -15.0
        self.on_play_start: Callable[[str], None] | None = None
        self.on_play_stop: Callable[[], None] | None = None
        self.on_volume_change: Callable[[float], None] | None = None

        # 事件监听端口与套接字
        self._notify_sock: socket.socket | None = None
        self._notify_port: int = 0
        self._notify_thread: threading.Thread | None = None
        self._running = False

        # FIFO 桥接与 Shairport Sync 进程
        self.bridge = FIFOAudioBridge(
            fifo_path=self.fifo_path,
            audio_stream_server=self.audio_stream_server,
            on_active_change=self._on_bridge_active_change,
        )
        self._sps_proc: subprocess.Popen | None = None

    @property
    def is_playing(self) -> bool:
        return self.bridge.is_streaming

    @property
    def stream_url(self) -> str:
        return self.audio_stream_server.stream_url

    async def start(self):
        """启动 AirPlay 2 接收服务"""
        log.info(f"[AirPlay 2] 正在启动服务: {self.device_name}")

        # 1. 启动本地 UDP 事件监听
        self._start_notify_listener()

        # 2. 启动 HTTP 音频流服务器
        await self.audio_stream_server.start()

        # 3. 启动 FIFO 桥接管道
        self.bridge.start()

        # 4. 生成 Shairport Sync 配置文件
        mdns_backend = "avahi" if sys.platform.startswith("linux") else "dns-sd"
        ShairportSyncConfig.generate(
            config_path=self.conf_path,
            device_name=self.device_name,
            rtsp_port=self.rtsp_port,
            fifo_path=self.fifo_path,
            notify_port=self._notify_port,
            mdns_backend=mdns_backend,
        )

        # 5. 检查并启动 Shairport Sync 外部进程
        self._start_shairport_sync()

        log.info(f"[AirPlay 2] 服务已就绪: {self.device_name}")
        log.info(f"[AirPlay 2]   RTSP 端口: {self.rtsp_port}")
        log.info(f"[AirPlay 2]   音频流: {self.stream_url}")

    async def stop(self):
        """停止 AirPlay 2 服务并清理全部资源"""
        log.info(f"[AirPlay 2] 正在停止服务: {self.device_name}")
        self._running = False

        # 1. 终止 Shairport Sync 进程
        if self._sps_proc:
            try:
                self._sps_proc.terminate()
                self._sps_proc.wait(timeout=1.5)
            except Exception:
                try:
                    self._sps_proc.kill()
                except Exception:
                    pass
            self._sps_proc = None

        # 2. 停止 FIFO 桥接
        self.bridge.stop()

        # 3. 停止 HTTP 音频流
        await self.audio_stream_server.stop()

        # 4. 关闭 UDP 通知套接字
        if self._notify_sock:
            try:
                self._notify_sock.close()
            except Exception:
                pass
            self._notify_sock = None

        # 5. 清理临时配置文件
        if os.path.exists(self.conf_path):
            try:
                os.remove(self.conf_path)
            except OSError:
                pass

        log.info(f"[AirPlay 2] 服务已完全停止: {self.device_name}")

    def _start_notify_listener(self):
        """启动用于接收 Shairport Sync hooks 事件的 UDP 本地监听"""
        self._notify_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._notify_sock.bind(("127.0.0.1", 0))
        self._notify_port = self._notify_sock.getsockname()[1]
        self._running = True

        def _listener_loop():
            while self._running:
                try:
                    data, _ = self._notify_sock.recvfrom(1024)
                    if not data:
                        continue
                    msg = data.decode("utf-8", errors="ignore").strip()
                    self._handle_notify_message(msg)
                except Exception:
                    break

        self._notify_thread = threading.Thread(
            target=_listener_loop, name="AP2-Notify-Listener", daemon=True
        )
        self._notify_thread.start()

    def _handle_notify_message(self, msg: str):
        """处理来自 Shairport Sync 的事件通知"""
        log.info(f"[AirPlay 2] 收到会话事件: {msg}")
        if msg == "play_begin":
            self.bridge.set_explicit_streaming(True)
        elif msg == "play_end":
            self.bridge.set_explicit_streaming(False)
        elif msg.startswith("volume"):
            parts = msg.split()
            if len(parts) >= 2:
                try:
                    vol_db = float(parts[1])
                    self._last_volume_db = vol_db
                    if self.on_volume_change:
                        self.on_volume_change(vol_db)
                except ValueError:
                    pass

    def _on_bridge_active_change(self, active: bool):
        """当音频流在活跃与空闲之间切换时的回调"""
        if active:
            log.info(f"[AirPlay 2] 触发音频流开启 -> 推流至音箱: {self.stream_url}")
            self.audio_stream_server.start_streaming()
            if self.on_play_start:
                self.on_play_start(self.stream_url)
        else:
            log.info(f"[AirPlay 2] 触发音频流关闭 -> 停止音箱播放")
            self.audio_stream_server.stop_streaming()
            if self.on_play_stop:
                self.on_play_stop()

    def _start_shairport_sync(self):
        """启动 Shairport Sync 进程（如果系统已安装）"""
        sps_bin = shutil.which("shairport-sync")
        if not sps_bin:
            log.warning(
                "[AirPlay 2] 系统未找到 shairport-sync 可执行文件。"
                "当前处于模拟桥接模式（可用于本地测试或等待外部进程接入）。"
            )
            return

        cmd = [sps_bin, "-c", self.conf_path]
        try:
            self._sps_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            log.info(
                f"[AirPlay 2] 已启动 Shairport Sync 进程 (PID: {self._sps_proc.pid})"
            )
        except Exception as e:
            log.error(f"[AirPlay 2] 启动 Shairport Sync 进程失败: {e}")

    # ============================================================
    # 模拟与测试辅助方法
    # ============================================================

    def inject_test_pcm(self, pcm_bytes: bytes):
        """向 FIFO 注入测试 PCM 音频数据（用于无实际 iPhone 时的端到端管道测试）"""
        if not os.path.exists(self.fifo_path):
            raise FileNotFoundError(f"FIFO 路径不存在: {self.fifo_path}")

        # 以写模式向 FIFO 发送数据
        fifo_fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fifo_fd, pcm_bytes)
        finally:
            os.close(fifo_fd)
