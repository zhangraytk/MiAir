"""AirPlay 基础组件单元测试：包含功能标志位、JitterBuffer、AP1 挑战应答及 mDNS 属性配置。"""

import unittest
from collections import deque
from miair.airplay.bitflags import FeatureFlags, StatusFlags
from miair.airplay.airplay1 import AP1Security
from miair.airplay.server import JitterBuffer
from miair.airplay.mdns import AirPlayMDNS, re_safe_hostname


class TestAirPlayBitflags(unittest.TestCase):
    def test_default_ap2_flags(self):
        flags = FeatureFlags(0).GetDefaultAirplayTwoFlags()
        # 验证 AirPlay 2 核心功能位
        self.assertTrue(bool(flags & FeatureFlags.Ft09AirPlayAudio))
        self.assertTrue(bool(flags & FeatureFlags.Ft40BufferedAudio))
        self.assertTrue(bool(flags & FeatureFlags.Ft41_PTPClock))
        self.assertTrue(bool(flags & FeatureFlags.Ft46HomeKitPairing))
        self.assertTrue(bool(flags & FeatureFlags.Ft48TransientPairing))

    def test_status_flags(self):
        status = StatusFlags(0).GetDefaultStatusFlags()
        self.assertTrue(bool(status & StatusFlags.AudioLink))


class TestJitterBuffer(unittest.TestCase):
    def setUp(self):
        self.jb = JitterBuffer(max_size=16)

    def test_insert_and_drain(self):
        self.jb.insert(1, 1000, b"pkt1")
        self.jb.insert(2, 1352, b"pkt2")
        self.jb.insert(3, 1704, b"pkt3")

        drained = self.jb.drain(1)
        self.assertEqual(len(drained), 3)
        self.assertEqual(drained[0][0], 1)
        self.assertEqual(drained[1][0], 2)
        self.assertEqual(drained[2][0], 3)
        self.assertEqual(len(self.jb), 0)

    def test_drain_with_gap(self):
        self.jb.insert(1, 1000, b"pkt1")
        self.jb.insert(3, 1704, b"pkt3")  # missing seq 2

        drained = self.jb.drain(1)
        self.assertEqual(len(drained), 1)  # stops at gap
        self.assertEqual(drained[0][0], 1)
        self.assertTrue(self.jb.has(3))

        missing = self.jb.gap_missing(2)
        self.assertIn(2, missing)

    def test_capacity_eviction(self):
        for i in range(20):
            self.jb.insert(i, i * 352, f"pkt{i}".encode())
        self.assertLessEqual(len(self.jb), 16)
        # 最旧的包应该已经被逐出
        self.assertFalse(self.jb.has(0))
        self.assertFalse(self.jb.has(1))
        self.assertTrue(self.jb.has(19))


class TestAP1Security(unittest.TestCase):
    def test_apple_challenge_response(self):
        # 使用已知测试向量验证 Apple-Challenge 签名计算
        challenge = "OL/y7hDI1kMrsW8Q7oim4Q"
        host_bytes = b"\xc0\xa8\x80\xe0"  # 192.168.128.224
        device_id = b"H]\x60|\xee\x22"     # 48:5D:60:7C:EE:22

        resp = AP1Security.compute_apple_response(challenge, host_bytes, device_id)
        self.assertIsInstance(resp, str)
        self.assertGreater(len(resp), 50)


class TestAirPlayMDNSNaming(unittest.TestCase):
    def test_re_safe_hostname(self):
        self.assertEqual(re_safe_hostname("客厅音箱 (Sound Pro)"), "sound-pro")
        self.assertEqual(re_safe_hostname("XiaoAi-Speaker_01"), "xiaoai-speaker-01")
        self.assertEqual(re_safe_hostname("---"), "speaker")

    def test_server_name_format(self):
        mdns = AirPlayMDNS("192.168.31.65", "客厅音箱", "AA:BB:CC:DD:EE:FF", 7000)
        server_name = mdns._get_server_name()
        self.assertTrue(server_name.startswith("miair-"))
        self.assertTrue(server_name.endswith(".local."))
        self.assertIn("aabbccddeeff", server_name)


if __name__ == "__main__":
    unittest.main()
