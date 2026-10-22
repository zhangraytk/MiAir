"""AirPlay 2 端到端音频串流测试：
验证 FIFO 注入 PCM -> FIFOAudioBridge -> AudioStreamServer -> HTTP 客户端拉流
"""

import asyncio
import os
import shutil
import tempfile
import unittest
import aiohttp

from miair.airplay.ap2_bridge import AirPlay2Server
from miair.config import Config


class TestAirPlay2E2EAudioStream(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="miair_e2e_ap2_")
        self.config = Config(conf_path=self.test_dir)
        self.server = AirPlay2Server(
            hostname="127.0.0.1",
            device_name="E2E_TestSpeaker",
            rtsp_port=7008,
            temp_dir=self.test_dir,
            config=self.config,
        )

        self.started_url = ""
        self.stopped_count = 0
        self.server.on_play_start = lambda url: setattr(self, "started_url", url)
        self.server.on_play_stop = lambda: setattr(self, "stopped_count", self.stopped_count + 1)

        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_pcm_to_http_stream_pipeline(self):
        """测试从 FIFO 注入 PCM 数据到 HTTP 客户端拉取 WAV 流的完整闭环"""
        # 1. 验证初始状态
        self.assertFalse(self.server.is_playing)
        stream_url = self.server.stream_url
        self.assertTrue(stream_url.startswith("http://127.0.0.1:"))

        # 2. 模拟真实 PCM 音频块 (44.1kHz, 16bit, 2ch -> 每秒 176400 字节)
        # 产生 0.2 秒的数据: 35280 字节
        pcm_chunk = b"\x01\x00\x02\x00" * (35280 // 4)

        # 3. 注入数据到 FIFO
        self.server.inject_test_pcm(pcm_chunk)
        await asyncio.sleep(0.3)

        # 4. 验证管道自动识别流启动
        self.assertTrue(self.server.is_playing)
        self.assertEqual(self.started_url, stream_url)

        # 5. 模拟小爱音箱通过 aiohttp 拉取 HTTP WAV 音频流
        received_bytes = bytearray()
        async with aiohttp.ClientSession() as session:
            async with session.get(stream_url) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "audio/wav")

                # 读取前 44 字节 WAV 头
                wav_header = await resp.content.read(44)
                self.assertEqual(len(wav_header), 44)
                self.assertEqual(wav_header[0:4], b"RIFF")
                self.assertEqual(wav_header[8:12], b"WAVE")
                self.assertEqual(wav_header[12:16], b"fmt ")
                self.assertEqual(wav_header[36:40], b"data")

                # 持续在后台再注入几个数据包
                for _ in range(5):
                    self.server.inject_test_pcm(pcm_chunk)
                    await asyncio.sleep(0.05)

                # 读取音频数据
                chunk = await resp.content.read(8192)
                received_bytes.extend(chunk)
                self.assertGreater(len(received_bytes), 0)

        # 6. 验证流能够平稳停止
        await self.server.stop()
        self.assertFalse(self.server.is_playing)

    async def test_bounded_queue_memory_safety(self):
        """压力测试：模拟高频写入与无消费者场景，验证有界缓冲队列严格防止内存无限累积"""
        # 激活流
        self.server.bridge.set_explicit_streaming(True)
        self.assertTrue(self.server.is_playing)

        # 连续写入 300 个 PCM 块 (远超队列上限 100)
        pcm_block = b"\x11\x22\x33\x44" * 1024
        for _ in range(300):
            self.server.audio_stream_server.write_pcm(pcm_block)

        # 验证底层音频队列长度被严格限制在 _QUEUE_MAXSIZE (100) 以内
        queue_size = self.server.audio_stream_server._audio_queue.qsize()
        self.assertLessEqual(queue_size, 100)


if __name__ == "__main__":
    unittest.main()
