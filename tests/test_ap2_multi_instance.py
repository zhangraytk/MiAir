"""AirPlay 2 多实例架构自动化测试：
涵盖：
1. 3 个 AirPlay 2 实例并发分配端口、Device ID、FIFO、通知套接字互不冲突
2. 3 个服务实例并发运行，单个实例退出不影响其他实例
3. 多实例 HTTP 音频流完全隔离，验证无串音
4. AirPlayManager 针对单个音箱的独立重启不影响其他音箱
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock
import aiohttp

from miair.airplay.ap2_bridge import AirPlay2Server
from miair.airplay.audio_stream import AudioStreamServer
from miair.airplay.speaker_airplay import AirPlayManager, SpeakerAirPlay
from miair.config import Config, Speaker


class TestAP2MultiInstance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="miair_multi_ap2_")
        self.config = Config(conf_path=self.test_dir)
        self.servers = []

    async def asyncTearDown(self):
        for s in self.servers:
            try:
                await s.stop()
            except Exception:
                pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_three_instances_resource_isolation(self):
        """测试 3 个实例端口、Device ID、FIFO 路径完全独立互不冲突"""
        dids = ["speaker_did_1", "speaker_did_2", "speaker_did_3"]
        ports = []
        device_ids = []
        macs = []
        fifo_paths = []
        conf_paths = []

        for did in dids:
            spk = self.config.get_speaker(did)
            spk.dlna_name = f"XiaoAi_{did}"
            port = self.config.allocate_airplay2_port(did)
            ports.append(port)
            device_ids.append(spk.get_shairport_device_id())
            macs.append(spk.get_airplay_mac())

            server = AirPlay2Server(
                hostname="127.0.0.1",
                device_name=spk.dlna_name,
                rtsp_port=port,
                temp_dir=self.test_dir,
                config=self.config,
                did=did,
                airplay_device_id=spk.get_shairport_device_id(),
                mock_mode=True,
            )
            fifo_paths.append(server.fifo_path)
            conf_paths.append(server.conf_path)

        # 1. 验证 3 个端口各不相同且连续递增
        self.assertEqual(len(set(ports)), 3, "RTSP 端口必须互不重复")
        self.assertEqual(ports, [7000, 7001, 7002])

        # 2. 验证 Device ID 基于 DID 稳定哈希且互不相同
        self.assertEqual(len(set(device_ids)), 3, "Device ID 必须互不重复")
        for dev_id in device_ids:
            self.assertTrue(dev_id.startswith("0x") and dev_id.endswith("L"))

        # 3. 验证 MAC 地址格式与独立性
        self.assertEqual(len(set(macs)), 3, "MAC 地址必须互不相同")
        for mac in macs:
            self.assertEqual(len(mac.split(":")), 6)

        # 4. 验证 FIFO 和配置文件路径按 DID 隔离
        self.assertEqual(len(set(fifo_paths)), 3, "FIFO 路径必须完全隔离")
        self.assertEqual(len(set(conf_paths)), 3, "Config 路径必须完全隔离")
        for did, fpath, cpath in zip(dids, fifo_paths, conf_paths):
            self.assertIn(did, fpath)
            self.assertIn(did, cpath)

    async def test_concurrent_three_servers_run_and_one_stops(self):
        """测试同时启动 3 个实例，一台退出，其余继续运行"""
        dids = ["did_alpha", "did_beta", "did_gamma"]
        for did in dids:
            spk = self.config.get_speaker(did)
            port = self.config.allocate_airplay2_port(did)
            server = AirPlay2Server(
                hostname="127.0.0.1",
                device_name=f"Spk_{did}",
                rtsp_port=port,
                temp_dir=self.test_dir,
                config=self.config,
                did=did,
                airplay_device_id=spk.get_shairport_device_id(),
                mock_mode=True,
            )
            await server.start()
            self.servers.append(server)

        # 验证 3 个服务均处于运行中状态
        for s in self.servers:
            self.assertTrue(s.is_running)
            self.assertIsNotNone(s.stream_url)
            self.assertTrue(os.path.exists(s.fifo_path))

        # 验证各自的 HTTP 音频流服务器端口各不相同
        http_ports = [s.audio_stream_server.port for s in self.servers]
        self.assertEqual(len(set(http_ports)), 3, "HTTP 流端口必须互不冲突")

        # 停止第二台音箱 (did_beta)
        await self.servers[1].stop()
        self.assertFalse(self.servers[1].is_running)

        # 第一台和第三台继续运行
        self.assertTrue(self.servers[0].is_running, "第一台音箱应继续正常运行")
        self.assertTrue(self.servers[2].is_running, "第三台音箱应继续正常运行")

        # 验证第一台音箱依然能注入和流转数据
        pcm_data = b"\x05\x00\x05\x00" * 1024
        self.servers[0].inject_test_pcm(pcm_data)
        await asyncio.sleep(0.1)
        self.assertTrue(self.servers[0].is_playing)

    async def test_no_cross_talk_between_http_audio_streams(self):
        """测试不同 HTTP 客户端接收各自音箱的流，验证无串音"""
        # 创建两个独立的音频流服务器
        stream1 = AudioStreamServer(hostname="127.0.0.1", port=0)
        stream2 = AudioStreamServer(hostname="127.0.0.1", port=0)
        await stream1.start()
        await stream2.start()

        stream1.start_streaming()
        stream2.start_streaming()

        try:
            # 音箱 1 发送特征字节 0xAA，音箱 2 发送特征字节 0x55
            chunk_a = b"\xaa" * 4096
            chunk_b = b"\x55" * 4096
            stream1.write_pcm(chunk_a)
            stream2.write_pcm(chunk_b)

            url1 = f"http://127.0.0.1:{stream1.port}/airplay/stream.wav"
            url2 = f"http://127.0.0.1:{stream2.port}/airplay/stream.wav"

            async with aiohttp.ClientSession() as session:
                async with session.get(url1) as resp1, session.get(url2) as resp2:
                    self.assertEqual(resp1.status, 200)
                    self.assertEqual(resp2.status, 200)

                    # 先读取 44 字节 WAV 头部
                    hdr1 = await resp1.content.read(44)
                    hdr2 = await resp2.content.read(44)
                    self.assertEqual(hdr1[:4], b"RIFF")
                    self.assertEqual(hdr2[:4], b"RIFF")

                    # 再读取实际音频内容
                    body1 = await resp1.content.read(4096)
                    body2 = await resp2.content.read(4096)

                    # 验证流 1 只包含 0xAA，绝不包含 0x55
                    self.assertIn(b"\xaa\xaa", body1)
                    self.assertNotIn(b"\x55\x55", body1, "音箱 1 流中绝不能出现音箱 2 的音频")

                    # 验证流 2 只包含 0x55，绝不包含 0xAA
                    self.assertIn(b"\x55\x55", body2)
                    self.assertNotIn(b"\xaa\xaa", body2, "音箱 2 流中绝不能出现音箱 1 的音频")
        finally:
            await stream1.stop()
            await stream2.stop()

    async def test_airplay_manager_isolated_restart(self):
        """测试 AirPlayManager 对单个音箱重启，其余音箱不受干扰"""
        manager = AirPlayManager(hostname="127.0.0.1", config=self.config)

        # 模拟两个音箱控制器
        mock_c1 = MagicMock()
        mock_c1.speaker = self.config.get_speaker("did_1")
        mock_c1.device_id = "did_1"
        mock_c1.play_by_url = AsyncMock()
        mock_c1.stop_play = AsyncMock()

        mock_c2 = MagicMock()
        mock_c2.speaker = self.config.get_speaker("did_2")
        mock_c2.device_id = "did_2"
        mock_c2.play_by_url = AsyncMock()
        mock_c2.stop_play = AsyncMock()

        spk_ap1 = SpeakerAirPlay("127.0.0.1", mock_c1, None, config=self.config)
        spk_ap2 = SpeakerAirPlay("127.0.0.1", mock_c2, None, config=self.config)

        # 注入 mock server
        mock_srv1 = MagicMock()
        mock_srv1.is_running = True
        mock_srv1.stop = AsyncMock()
        spk_ap1.airplay_server = mock_srv1

        mock_srv2 = MagicMock()
        mock_srv2.is_running = True
        mock_srv2.stop = AsyncMock()
        spk_ap2.airplay_server = mock_srv2

        manager.speaker_airplays["did_1"] = spk_ap1
        manager.speaker_airplays["did_2"] = spk_ap2

        # 执行重启 did_1（使用 patch SpeakerAirPlay.start 避免拉起真实服务）
        with unittest.mock.patch("miair.airplay.speaker_airplay.SpeakerAirPlay.start", new_callable=AsyncMock) as mock_start:
            ok = await manager.restart_speaker("did_1", mock_c1)
            self.assertTrue(ok)
            mock_srv1.stop.assert_awaited_once()
            mock_start.assert_awaited_once()

        # 验证 did_2 完全未被 stop
        mock_srv2.stop.assert_not_called()
        self.assertTrue(manager.speaker_airplays["did_2"].airplay_server.is_running)


if __name__ == "__main__":
    unittest.main()
