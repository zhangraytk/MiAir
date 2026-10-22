"""Mock AirPlay 发送端模拟测试脚本。

无需真实 iPhone 或小爱音箱硬件，模拟完整 RTSP 会话与 RTP 数据流交互：
1. 启动本地独立的 AirPlayServer
2. 发送 OPTIONS（包含 Apple-Challenge 认证请求）
3. 发送 ANNOUNCE（包含 SDP 音频描述）
4. 发送 SETUP（协商传输端口 server_port/control_port/timing_port）
5. 发送 RECORD（启动推流）
6. 通过 UDP 向 server_port 发送模拟音频包
7. 发送 TEARDOWN 正常结束播放
8. 验证服务状态重置
"""

import asyncio
import socket
import time
import unittest
from unittest.mock import MagicMock

from miair.airplay.server import AirPlayServer


class MockAirPlaySender:
    """AirPlay RTSP 客户端模拟器"""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.cseq = 0

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5.0)
        self.sock.connect((self.host, self.port))

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _send_request(self, method: str, path: str = "rtsp://localhost/stream", headers: dict | None = None, body: bytes = b"") -> tuple[int, dict, bytes]:
        self.cseq += 1
        req_headers = {
            "CSeq": str(self.cseq),
            "User-Agent": "MockAirPlaySender/1.0",
        }
        if headers:
            req_headers.update(headers)
        if body:
            req_headers["Content-Length"] = str(len(body))

        lines = [f"{method} {path} RTSP/1.0"]
        for k, v in req_headers.items():
            lines.append(f"{k}: {v}")
        req_bytes = "\r\n".join(lines).encode() + b"\r\n\r\n" + body

        self.sock.sendall(req_bytes)

        # 读取响应
        res_data = b""
        while b"\r\n\r\n" not in res_data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            res_data += chunk

        header_end = res_data.find(b"\r\n\r\n")
        header_text = res_data[:header_end].decode("utf-8", errors="replace")
        res_body = res_data[header_end + 4:]

        lines = header_text.split("\r\n")
        status_line = lines[0]
        status_code = int(status_line.split()[1])

        resp_headers = {}
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1)
                resp_headers[k.strip()] = v.strip()

        content_len = int(resp_headers.get("Content-Length", 0))
        while len(res_body) < content_len:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            res_body += chunk

        return status_code, resp_headers, res_body

    def options(self, challenge: str = "") -> tuple[int, dict]:
        headers = {}
        if challenge:
            headers["Apple-Challenge"] = challenge
        status, resp_headers, _ = self._send_request("OPTIONS", "*", headers)
        return status, resp_headers

    def announce(self, sdp_body: str) -> tuple[int, dict]:
        headers = {"Content-Type": "application/sdp"}
        status, resp_headers, _ = self._send_request("ANNOUNCE", headers=headers, body=sdp_body.encode())
        return status, resp_headers

    def setup(self, control_port: int = 6001, timing_port: int = 6002) -> tuple[int, dict]:
        transport = f"RTP/AVP/UDP;unicast;interleaved=0-1;mode=record;control_port={control_port};timing_port={timing_port}"
        headers = {"Transport": transport}
        status, resp_headers, _ = self._send_request("SETUP", headers=headers)
        return status, resp_headers

    def record(self) -> tuple[int, dict]:
        headers = {
            "Range": "npt=0-",
            "RTP-Info": "seq=1;rtptime=0",
        }
        status, resp_headers, _ = self._send_request("RECORD", headers=headers)
        return status, resp_headers

    def teardown(self) -> tuple[int, dict]:
        status, resp_headers, _ = self._send_request("TEARDOWN")
        return status, resp_headers


class TestMockAirPlaySession(unittest.IsolatedAsyncioTestCase):
    """端到端模拟测试"""

    async def asyncSetUp(self):
        # 使用 mock zeroconf，避免广播干扰
        self.mock_zc = MagicMock()
        self.server = AirPlayServer(
            hostname="127.0.0.1",
            device_name="TestVirtualSpeaker",
            shared_zeroconf=self.mock_zc,
            speaker_hardware="mock",
        )
        await self.server.start()
        self.client = MockAirPlaySender("127.0.0.1", self.server.rtsp_port)
        self.client.connect()

    async def asyncTearDown(self):
        self.client.close()
        await self.server.stop()

    async def test_full_airplay_handshake(self):
        # 1. OPTIONS + Apple-Challenge
        challenge = "OL/y7hDI1kMrsW8Q7oim4Q"
        status, headers = self.client.options(challenge=challenge)
        self.assertEqual(status, 200)
        self.assertIn("Public", headers)
        self.assertIn("Apple-Response", headers)
        self.assertTrue(len(headers["Apple-Response"]) > 20)

        # 2. ANNOUNCE with PCM/ALAC SDP
        sdp = (
            "v=0\r\n"
            "o=iTunes 1234567 0 IN IP4 127.0.0.1\r\n"
            "s=iTunes\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            "m=audio 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/44100/2\r\n"
            "i=MockSenderDevice\r\n"
        )
        status, _ = self.client.announce(sdp)
        self.assertEqual(status, 200)
        self.assertTrue(self.server.is_playing)
        self.assertEqual(self.server.client_name, "MockSenderDevice")

        # 3. SETUP
        status, headers = self.client.setup(control_port=7100, timing_port=7101)
        self.assertEqual(status, 200)
        self.assertIn("Transport", headers)
        transport = headers["Transport"]
        self.assertIn("server_port=", transport)
        self.assertIn("control_port=", transport)

        # 解析服务端分配的 UDP RTP 端口
        server_port = None
        for part in transport.split(";"):
            if part.startswith("server_port="):
                server_port = int(part.split("=")[1])
        self.assertIsNotNone(server_port)

        # 4. RECORD
        status, _ = self.client.record()
        self.assertEqual(status, 200)

        # 5. 发送测试音频 UDP 包 (16-bit 44100Hz 2ch 静音包)
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # RTP 基础报头: V=2, P=0, X=0, CC=0, M=0, PT=96, Seq=1, TS=0, SSRC=0x12345678
        rtp_header = bytes([0x80, 0x60, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x12, 0x34, 0x56, 0x78])
        pcm_payload = b"\x00" * 352 * 4  # 352 samples, 16bit, stereo
        udp_sock.sendto(rtp_header + pcm_payload, ("127.0.0.1", server_port))
        udp_sock.close()

        # 等待后台工作线程处理
        await asyncio.sleep(0.1)

        # 6. TEARDOWN
        status, _ = self.client.teardown()
        self.assertEqual(status, 200)
        self.assertFalse(self.server.is_playing)


if __name__ == "__main__":
    unittest.main()
