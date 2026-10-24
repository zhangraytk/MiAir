"""AirPlay 2 桥接服务及 FIFO 管道自动化单元测试"""

import asyncio
import os
import shutil
import socket
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from miair.airplay.ap2_bridge import (
    FIFOAudioBridge,
    ShairportSyncConfig,
    AirPlay2Server,
    sanitize_filename,
)
from miair.airplay.audio_stream import AudioStreamServer
from miair.config import Config


class TestAP2BridgeComponents(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="miair_test_ap2_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("Living Room - Speaker (Pro)"), "Living_Room_-_Speaker__Pro_")
        self.assertEqual(sanitize_filename("XiaoAi.01_A-B"), "XiaoAi.01_A-B")

    def test_shairport_sync_config_generation(self):
        conf_path = os.path.join(self.test_dir, "test.conf")
        fifo_path = os.path.join(self.test_dir, "test.pcm")
        ShairportSyncConfig.generate(
            config_path=conf_path,
            device_name="Test Speaker [AP2]",
            rtsp_port=7001,
            fifo_path=fifo_path,
            notify_port=55555,
        )

        self.assertTrue(os.path.exists(conf_path))
        with open(conf_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('name = "Test Speaker [AP2]";', content)
        self.assertIn('output_backend = "pipe";', content)
        self.assertIn("port = 7001;", content)
        self.assertIn(f'name = "{fifo_path}";', content)
        self.assertIn('output_format = "S16";', content)
        self.assertIn('output_rate = 44100;', content)
        self.assertIn("55555 play_begin", content)
        self.assertIn("55555 play_end", content)
        self.assertIn("55555 volume", content)
        self.assertIn("audio_backend_latency_offset_in_seconds = 0.000;", content)

    def test_shairport_latency_offset_units_and_direction(self):
        conf_path = os.path.join(self.test_dir, "offset.conf")
        for milliseconds, seconds in [
            (-10000, "-10.000"),
            (-4500, "-4.500"),
            (-2000, "-2.000"),
            (-250, "-0.250"),
            (350, "0.350"),
            (2000, "2.000"),
            (5000, "5.000"),
        ]:
            with self.subTest(milliseconds=milliseconds):
                ShairportSyncConfig.generate(
                    conf_path, "Test", 7000, os.path.join(self.test_dir, "audio.pcm"),
                    55555, latency_offset_ms=milliseconds,
                )
                with open(conf_path, encoding="utf-8") as f:
                    self.assertIn(f"audio_backend_latency_offset_in_seconds = {seconds};", f.read())

    def test_latency_config_persistence_and_default(self):
        config = Config(conf_path=self.test_dir, hostname="127.0.0.1")
        self.assertEqual(config.airplay2_latency_offset_ms, 0)
        config.airplay2_latency_offset_ms = -250
        config.save()
        self.assertEqual(Config.load(self.test_dir).airplay2_latency_offset_ms, -250)


class TestFIFOAudioPipeline(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="miair_fifo_test_")
        self.fifo_path = os.path.join(self.test_dir, "pipe.pcm")
        self.mock_audio_server = MagicMock(spec=AudioStreamServer)
        self.active_events = []

        def on_active(active: bool):
            self.active_events.append(active)

        self.bridge = FIFOAudioBridge(
            fifo_path=self.fifo_path,
            audio_stream_server=self.mock_audio_server,
            on_active_change=on_active,
            chunk_size=1024,
            silence_timeout=0.6,  # 缩短超时时间以便快速测试
        )

    def tearDown(self):
        self.bridge.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_fifo_data_flow_and_activity_sensing(self):
        self.bridge.start()
        self.assertTrue(os.path.exists(self.fifo_path))
        self.assertFalse(self.bridge.is_streaming)

        # 向 FIFO 写入一段合成的 44100Hz 16-bit 立体声静音/正弦波数据 (2048 字节)
        test_pcm = b"\x00\x00\x10\x00" * 512
        fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, test_pcm)
        finally:
            os.close(fd)

        # 等待后台读取线程消费
        time.sleep(0.3)
        self.assertTrue(self.bridge.is_streaming)
        self.assertIn(True, self.active_events)
        self.mock_audio_server.write_pcm.assert_called()

        # 等待 silence_timeout 超时，验证自动判定流结束
        time.sleep(0.8)
        self.assertFalse(self.bridge.is_streaming)
        self.assertIn(False, self.active_events)


class TestAirPlay2ServerNotifications(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="miair_server_test_")
        self.config = Config(conf_path=self.test_dir, airplay2_latency_offset_ms=-250)
        self.server = AirPlay2Server(
            hostname="127.0.0.1",
            device_name="MockSpeaker",
            rtsp_port=7005,
            temp_dir=self.test_dir,
            config=self.config,
            mock_mode=True,
            stop_debounce_seconds=0.08,
        )

        self.play_started_urls = []
        self.play_stopped_calls = 0
        self.volume_changes = []

        self.server.on_play_start = lambda url: self.play_started_urls.append(url)
        self.server.on_play_stop = lambda: setattr(self, "play_stopped_calls", self.play_stopped_calls + 1)
        self.server.on_volume_change = lambda vol: self.volume_changes.append(vol)

        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_udp_session_hooks(self):
        # 验证服务接口契约
        self.assertEqual(self.server.rtsp_port, 7005)
        self.assertTrue(self.server.device_name.endswith("[AP2]"))
        self.assertIn("127.0.0.1", self.server.stream_url)

        # 向 UDP 监听端口发送 play_begin 事件
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = self.server._notify_port
        s.sendto(b"play_begin", ("127.0.0.1", port))
        await asyncio.sleep(0.05)

        self.assertTrue(self.server.is_playing)
        self.assertEqual(len(self.play_started_urls), 1)

        # 发送音量事件
        s.sendto(b"volume -12.5", ("127.0.0.1", port))
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.volume_changes), 1)
        self.assertAlmostEqual(self.volume_changes[0], -12.5)

        # 发送 play_end 事件，等待防抖宽限期 0.08s 结束
        s.sendto(b"play_end", ("127.0.0.1", port))
        await asyncio.sleep(0.15)
        self.assertFalse(self.server.is_playing)
        self.assertEqual(self.play_stopped_calls, 1)

        s.close()

    async def test_stop_debounce_cancels_on_group_renegotiation(self):
        """模拟原生 HomePod 后加入时 iOS 的瞬间暂停/重协商：
        发送 play_end 后立即进入防抖，并在宽限期内收到恢复信号，
        验证音箱保持播放状态，未触发 on_play_stop 断开连接。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = self.server._notify_port

        # 1. 启动播放
        s.sendto(b"play_begin", ("127.0.0.1", port))
        await asyncio.sleep(0.05)
        self.assertTrue(self.server.is_playing)
        self.assertEqual(len(self.play_started_urls), 1)

        # 2. 模拟加入 HomePod 触发的短暂 play_end (组重协商)
        s.sendto(b"play_end", ("127.0.0.1", port))
        await asyncio.sleep(0.03)  # 小于 0.08s 防抖阈值

        # 此时应仍在防抖宽限期中，仍保持 is_playing 为 True，且尚未调用 on_play_stop
        self.assertTrue(self.server.is_playing)
        self.assertEqual(self.play_stopped_calls, 0)

        # 3. iOS 重协商完成，发送 play_begin 或继续推流
        s.sendto(b"play_begin", ("127.0.0.1", port))
        # 等待超过初始防抖期限
        await asyncio.sleep(0.1)

        # 验证防抖已成功被取消，未停止音箱，无缝保持播放
        self.assertTrue(self.server.is_playing)
        self.assertEqual(self.play_stopped_calls, 0)
        self.assertEqual(len(self.play_started_urls), 1, "同一会话无需重复通知 play_url")

        s.close()

    async def test_server_passes_latency_to_generated_config(self):
        with open(self.server.conf_path, encoding="utf-8") as f:
            self.assertIn("audio_backend_latency_offset_in_seconds = -0.250;", f.read())


class TestSpeakerAirPlayModeSwitching(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="miair_spk_test_")
        self.mock_controller = MagicMock()
        self.mock_controller.speaker.get_dlna_name.return_value = "Living Room"
        self.mock_controller.speaker.hardware = "L05A"
        self.mock_controller.speaker.did = "123456"

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_legacy_mode_initializes_airplay1(self):
        from miair.airplay.speaker_airplay import SpeakerAirPlay
        from miair.airplay.server import AirPlayServer

        config = Config(conf_path=self.test_dir, airplay_mode="legacy")
        spk_ap = SpeakerAirPlay(
            hostname="127.0.0.1",
            controller=self.mock_controller,
            config=config,
        )
        await spk_ap.start()
        try:
            self.assertIsInstance(spk_ap.airplay_server, AirPlayServer)
        finally:
            await spk_ap.stop()

    async def test_experimental_mode_initializes_airplay2(self):
        from miair.airplay.speaker_airplay import SpeakerAirPlay
        from miair.airplay.ap2_bridge import AirPlay2Server

        config = Config(conf_path=self.test_dir, airplay_mode="experimental")
        config.airplay2_mock_mode = True
        spk_ap = SpeakerAirPlay(
            hostname="127.0.0.1",
            controller=self.mock_controller,
            config=config,
        )
        await spk_ap.start()
        try:
            self.assertIsInstance(spk_ap.airplay_server, AirPlay2Server)
            self.assertTrue(spk_ap.airplay_server.device_name.endswith("[AP2]"))
        finally:
            await spk_ap.stop()

    async def test_experimental_mode_failure_does_not_silently_fallback(self):
        """验证实验模式下启动失败时明确报错，严禁静默回退到 AirPlay 1 造成假阳性"""
        from unittest.mock import patch
        from miair.airplay.speaker_airplay import SpeakerAirPlay
        from miair.airplay.ap2_bridge import AirPlay2Server

        config = Config(conf_path=self.test_dir, airplay_mode="experimental")
        spk_ap = SpeakerAirPlay(
            hostname="127.0.0.1",
            controller=self.mock_controller,
            config=config,
        )

        # 模拟 AirPlay2Server 启动崩溃 (如 shairport-sync 启动异常)
        with patch.object(AirPlay2Server, "start", side_effect=RuntimeError("Shairport Sync config error")):
            with self.assertRaises(RuntimeError):
                await spk_ap.start()

        # 验证失败后未静默启动 AirPlay 1 服务
        self.assertIsNone(spk_ap.airplay_server)


class TestWebAPIAirPlaySettings(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="miair_web_test_")
        self.config = Config(conf_path=self.test_dir, airplay_mode="experimental", airplay2_port=7002)

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_get_setting_contains_airplay_mode(self):
        from aiohttp.test_utils import make_mocked_request
        from miair.web.api import create_web_app

        mock_app = MagicMock()
        mock_app.dlna_running = False
        mock_app.renderers = []

        web_app = create_web_app(self.config, mock_app)
        # 查找 handle_get_setting handler
        for route in web_app.router.routes():
            if route.method == "GET" and "/api/setting" in str(route.resource):
                req = make_mocked_request("GET", "/api/setting?need_device_list=false")
                resp = await route.handler(req)
                import json
                data = json.loads(resp.text)
                self.assertIn("airplay_mode", data)
                self.assertEqual(data["airplay_mode"], "experimental")
                self.assertIn("airplay2_port", data)
                self.assertEqual(data["airplay2_port"], 7002)
                self.assertEqual(data["airplay2_latency_offset_ms"], 0)
                break

    async def test_latency_setting_save_and_read_back(self):
        from aiohttp.test_utils import TestClient, TestServer
        from miair.web.api import create_web_app

        app = MagicMock(dlna_running=False, renderers=[])
        async with TestClient(TestServer(create_web_app(self.config, app))) as client:
            with patch("miair.web.api._restart_process") as restart:
                for offset in (-10000, -3500, -250, 0, 350, 4500):
                    with self.subTest(offset=offset):
                        response = await client.post("/api/setting", json={"airplay2_latency_offset_ms": offset})
                        self.assertEqual(response.status, 200)
                        self.assertTrue((await response.json())["ok"])
                        response = await client.get("/api/setting")
                        self.assertEqual((await response.json())["airplay2_latency_offset_ms"], offset)
                        self.assertEqual(Config.load(self.test_dir).airplay2_latency_offset_ms, offset)
                await asyncio.sleep(0)
                self.assertEqual(restart.call_count, 6)

    async def test_invalid_latency_does_not_change_or_save_config(self):
        from aiohttp.test_utils import TestClient, TestServer
        from miair.web.api import create_web_app

        app = MagicMock(dlna_running=False, renderers=[])
        async with TestClient(TestServer(create_web_app(self.config, app))) as client:
            with patch.object(self.config, "save") as save, patch("miair.web.api._restart_process") as restart:
                for offset in (-15001, 5001, 1.5, "-250", None, True):
                    with self.subTest(offset=offset):
                        response = await client.post("/api/setting", json={
                            "airplay2_latency_offset_ms": offset, "airplay_mode": "legacy",
                        })
                        self.assertEqual(response.status, 400)
                        self.assertEqual(self.config.airplay2_latency_offset_ms, 0)
                        self.assertEqual(self.config.airplay_mode, "experimental")
                save.assert_not_called()
                restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
