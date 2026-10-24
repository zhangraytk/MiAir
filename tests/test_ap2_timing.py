"""AirPlay 2 时钟与下游 HTTP 时间观测自动化测试：
涵盖：
1. 记录高精度单调时钟时间线：会话启动 -> PCM 首包到达 -> 音箱连接 -> 首音频发送
2. 观测指标结构完备性与数据准确性 (queue depth, buffer ms, 溢出/欠载/重连计数)
3. 模拟音箱客户端短开重连及重连计数递增
4. 队列缓冲时长精确计算 (176400 字节/秒 = 44.1kHz 16-bit 2ch)
"""

import asyncio
import time
import unittest
import aiohttp

from miair.airplay.audio_stream import AudioStreamServer


class TestAP2Timing(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stream_server = AudioStreamServer(
            hostname="127.0.0.1",
            port=0,
            sync_debug=True,
        )
        await self.stream_server.start()

    async def asyncTearDown(self):
        await self.stream_server.stop()

    async def test_timing_timeline_monotonic_progression(self):
        """测试各时间锚点严格遵循单调递增时序：会话 -> PCM到达 -> 音箱拉流 -> 首音频发出"""
        # 1. 启动会话
        t0 = time.monotonic()
        self.stream_server.start_streaming()
        stats0 = self.stream_server.get_timing_stats()
        self.assertGreaterEqual(stats0["session_start_time"], t0)
        self.assertEqual(stats0["pcm_first_data_time"], 0.0)
        self.assertEqual(stats0["speaker_http_connected_time"], 0.0)
        self.assertEqual(stats0["first_audio_sent_time"], 0.0)

        await asyncio.sleep(0.05)

        # 2. 模拟首批 PCM 数据流入
        t_pcm = time.monotonic()
        pcm_chunk = b"\x01\x00\x02\x00" * 4410  # 0.1s 音频 (17640 字节)
        self.stream_server.write_pcm(pcm_chunk)
        stats1 = self.stream_server.get_timing_stats()
        self.assertGreaterEqual(stats1["pcm_first_data_time"], t_pcm)
        self.assertGreaterEqual(stats1["pcm_first_data_time"], stats0["session_start_time"])

        await asyncio.sleep(0.05)

        # 3. 模拟音箱建立 HTTP 连接并拉流
        url = f"http://127.0.0.1:{self.stream_server.port}/airplay/stream.wav"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                self.assertEqual(resp.status, 200)

                # 读取 WAV 头部与数据
                hdr = await resp.content.read(44)
                self.assertEqual(len(hdr), 44)
                data = await resp.content.read(4096)
                self.assertGreater(len(data), 0)

                stats2 = self.stream_server.get_timing_stats()
                # 验证连接时间与首音频发出时间
                self.assertGreater(stats2["speaker_http_connected_time"], 0.0)
                self.assertGreater(stats2["first_audio_sent_time"], 0.0)

                # 验证绝对单调时钟推进
                self.assertLessEqual(stats2["session_start_time"], stats2["pcm_first_data_time"])
                self.assertLessEqual(stats2["pcm_first_data_time"], stats2["speaker_http_connected_time"])
                self.assertLessEqual(stats2["speaker_http_connected_time"], stats2["first_audio_sent_time"])

    async def test_timing_stats_completeness_and_buffer_calculation(self):
        """测试综合时序指标字段完整性及音频缓冲时长换算"""
        self.stream_server.start_streaming()

        # 写入 0.2 秒的 PCM 数据: 44100 * 2ch * 2bytes * 0.2s = 35280 字节
        data_0_2s = b"\x00" * 35280
        self.stream_server.write_pcm(data_0_2s)

        stats = self.stream_server.get_timing_stats()
        required_keys = [
            "session_start_time",
            "pcm_first_data_time",
            "speaker_http_connected_time",
            "first_audio_sent_time",
            "elapsed_since_session_start_sec",
            "audio_queue_depth",
            "buffer_duration_ms",
            "overflow_count",
            "underflow_count",
            "dropped_frames",
            "stop_count",
            "reconnect_count",
            "latency_offset_ms",
            "prebuffer_ms",
        ]
        for key in required_keys:
            self.assertIn(key, stats, f"指标中必须包含 {key}")

        # 验证队列深度为 1
        self.assertEqual(stats["audio_queue_depth"], 1)
        # 验证计算出的缓冲时长近似为 200 ms (允许采样对齐误差 within 2ms)
        self.assertAlmostEqual(stats["buffer_duration_ms"], 200.0, delta=2.0)

    async def test_reconnect_and_stop_counter(self):
        """测试音箱短开重连及停止会话时计数器的统计"""
        self.stream_server.start_streaming()
        pcm_chunk = b"\x10\x00\x10\x00" * 4410

        url = f"http://127.0.0.1:{self.stream_server.port}/airplay/stream.wav"

        # 第一次连接
        self.stream_server.write_pcm(pcm_chunk)
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                await resp.content.read(44)
                await resp.content.read(1024)

        stats_after_c1 = self.stream_server.get_timing_stats()
        self.assertEqual(stats_after_c1["reconnect_count"], 0, "首次连接不计入重连")

        # 第二次连接（模拟音箱被临时打断后自动重连）
        self.stream_server.write_pcm(pcm_chunk)
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                await resp.content.read(44)
                await resp.content.read(1024)

        stats_after_c2 = self.stream_server.get_timing_stats()
        self.assertEqual(stats_after_c2["reconnect_count"], 1, "再次拉流应计为 1 次重连")

        # 停止流
        self.stream_server.stop_streaming()
        stats_stopped = self.stream_server.get_timing_stats()
        self.assertEqual(stats_stopped["stop_count"], 1)


if __name__ == "__main__":
    unittest.main()
