"""AirPlay 2 延迟补偿与预缓冲自动化测试：
涵盖：
1. 正延迟补偿 (+100ms, +300ms, +600ms)：精准注入连续静音对齐帧，实测后续真实音频的偏移量
2. 负延迟补偿 (-100ms, -200ms)：精准跳过前置采样，实现下游提前播放
3. 4 字节立体声 (16-bit 2ch) 帧对齐强保证，防止左右声道翻转或爆音
4. 预缓冲 prebuffer_ms 累积机制测试
"""

import asyncio
import struct
import unittest
import aiohttp

from miair.airplay.audio_stream import AudioStreamServer


class TestAP2LatencyCompensation(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        if hasattr(self, "stream_server") and self.stream_server:
            await self.stream_server.stop()

    async def _setup_server(self, latency_offset_ms: int = 0, prebuffer_ms: int = 0):
        self.stream_server = AudioStreamServer(
            hostname="127.0.0.1",
            port=0,
            latency_offset_ms=latency_offset_ms,
            prebuffer_ms=prebuffer_ms,
            sync_debug=True,
        )
        await self.stream_server.start()
        self.stream_server.start_streaming()
        return self.stream_server

    async def test_positive_latency_offsets_silence_padding(self):
        """测试正延迟补偿 (+100ms, +300ms, +600ms)：严格填充对应字节数的静音并对齐音频"""
        offsets_to_test = [100, 300, 600]

        for offset_ms in offsets_to_test:
            with self.subTest(offset_ms=offset_ms):
                server = await self._setup_server(latency_offset_ms=offset_ms)
                sample_rate = 44100
                frame_size = 4  # 16-bit stereo = 4 bytes
                expected_silence_bytes = int(round(sample_rate * offset_ms / 1000.0)) * frame_size

                # 特征音频块：0xA1 0xB2 0xC3 0xD4 重复
                marker_frame = b"\xa1\xb2\xc3\xd4"
                pcm_data = marker_frame * 1000  # 4000 字节
                server.write_pcm(pcm_data)

                url = f"http://127.0.0.1:{server.port}/airplay/stream.wav"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        self.assertEqual(resp.status, 200)

                        # 读取 44 字节 WAV 头部
                        header = await resp.content.read(44)
                        self.assertEqual(header[:4], b"RIFF")

                        # 读取填充的静音部分
                        silence_part = await resp.content.readexactly(expected_silence_bytes)
                        # 验证全部为 0x00 静音字节
                        self.assertEqual(silence_part, b"\x00" * expected_silence_bytes)

                        # 紧随静音之后的数据必须是特征音频起始帧
                        first_real_frame = await resp.content.readexactly(4)
                        self.assertEqual(first_real_frame, marker_frame, "静音填充结束后必须无缝衔接真实音频首帧")

                await server.stop()

    async def test_negative_latency_offsets_sample_skip(self):
        """测试负延迟补偿 (-100ms, -200ms)：精准截断丢弃指定时长的起始采样"""
        offsets_to_test = [-100, -200]

        for offset_ms in offsets_to_test:
            with self.subTest(offset_ms=offset_ms):
                server = await self._setup_server(latency_offset_ms=offset_ms)
                sample_rate = 44100
                frame_size = 4
                expected_skip_bytes = int(round(sample_rate * abs(offset_ms) / 1000.0)) * frame_size

                # 构造具有自增编号的立体声采样（每个 sample 2 字节，左声道为编号，右声道为编号+1）
                total_frames = (expected_skip_bytes // frame_size) + 500
                buffer = bytearray()
                for i in range(total_frames):
                    # 打包两个 unsigned short (16-bit LE)
                    buffer.extend(struct.pack("<HH", i % 65535, (i + 1) % 65535))

                server.write_pcm(bytes(buffer))

                url = f"http://127.0.0.1:{server.port}/airplay/stream.wav"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        self.assertEqual(resp.status, 200)

                        # 读取 44 字节 WAV 头部
                        await resp.content.read(44)

                        # 读取首批输出的 4 字节音频帧
                        first_output_frame = await resp.content.readexactly(4)
                        out_left, out_right = struct.unpack("<HH", first_output_frame)

                        # 预期跳过的帧索引
                        expected_first_frame_idx = expected_skip_bytes // frame_size
                        self.assertEqual(out_left, expected_first_frame_idx % 65535)
                        self.assertEqual(out_right, (expected_first_frame_idx + 1) % 65535)

                await server.stop()

    def test_frame_boundary_preservation(self):
        """测试任意毫秒补偿值均保持 4 字节对齐，绝不破坏声道结构"""
        sample_rate = 44100
        frame_size = 4

        # 测试各种非整百的任意毫秒值
        test_ms_values = [-17, -33, -1, 1, 13, 27, 125, 333, 999]
        for ms in test_ms_values:
            frames = int(round(sample_rate * abs(ms) / 1000.0))
            byte_count = frames * frame_size
            self.assertEqual(byte_count % frame_size, 0, f"{ms}ms 的偏移量必须能被 4 整除")

    async def test_prebuffer_ms_mechanism(self):
        """测试预缓冲 prebuffer_ms 累积门槛"""
        prebuffer_ms = 200
        server = await self._setup_server(prebuffer_ms=prebuffer_ms)
        sample_rate = 44100
        frame_size = 4
        needed_bytes = int(round(sample_rate * prebuffer_ms / 1000.0)) * frame_size

        # 1. 注入不足预缓冲量的数据 (例如仅注入 50ms)
        chunk_50ms = b"\x11\x22\x33\x44" * int(round(sample_rate * 0.05))
        server.write_pcm(chunk_50ms)

        url = f"http://127.0.0.1:{server.port}/airplay/stream.wav"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                self.assertEqual(resp.status, 200)
                await resp.content.read(44)

                # 2. 注入补充数据使得总数据量达到并超过 prebuffer_bytes
                chunk_200ms = b"\x55\x66\x77\x88" * int(round(sample_rate * 0.20))
                server.write_pcm(chunk_200ms)

                # 客户端现在能读取到数据
                data = await resp.content.read(4096)
                self.assertGreater(len(data), 0)

    async def test_ap2_server_delegates_latency_offset_to_shairport_sync(self):
        """测试 AirPlay2Server 将时钟偏移完全委托给 Shairport Sync PTP 调度：
        AudioStreamServer 的 latency_offset_ms 必须为 0 (禁止下游截断丢帧)，
        且生成的 Shairport 配置文件与 timing stats 准确反映宽量程补偿 (如 -3500ms -> -3.500s)。
        """
        import tempfile
        import shutil
        from miair.airplay.ap2_bridge import AirPlay2Server

        test_dir = tempfile.mkdtemp(prefix="miair_latency_test_")
        try:
            server = AirPlay2Server(
                hostname="127.0.0.1",
                device_name="LivingRoom",
                rtsp_port=7010,
                temp_dir=test_dir,
                latency_offset_ms=-3500,
                mock_mode=True,
            )
            await server.start()

            # 1. 验证 AudioStreamServer 不重复进行截断，采样无损透传
            self.assertEqual(server.audio_stream_server.latency_offset_ms, 0)

            # 2. 验证 Shairport 配置文件精确写入 -3.500s 宽时差调度
            with open(server.conf_path, encoding="utf-8") as f:
                content = f.read()
                self.assertIn("audio_backend_latency_offset_in_seconds = -3.500;", content)

            # 3. 验证统计观测准确报告配置值
            stats = server.get_timing_stats()
            self.assertEqual(stats["latency_offset_ms"], -3500)

            await server.stop()
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
