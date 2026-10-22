# MiAir Agent 开发与测试指南 (AirPlay 2 适配专项)

本项目致力于将小米小爱音箱（如 Xiaomi Sound Pro、小爱音箱 Play 增强版等）虚拟为高品质网络音乐播放终端。
当前代码库包含 DLNA 与 AirPlay 1 (RAOP) 实现，本目录核心演进目标为**适配与升级至 AirPlay 2 协议**（支持多房间立体声同步、高品质缓冲串流、PTP 时钟同步与现代 iOS/macOS 隔空播放体验）。

---

## 1. 架构总览与模块职责

```
                ┌────────────────────────────────────────────────────────┐
                │             iOS / macOS / iTunes 发送端                │
                └──────────────────────────┬─────────────────────────────┘
                                           │
                    [mDNS 广播发现: _airplay._tcp / _raop._tcp]
                                           ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ miair (核心协议与服务编排)                                                                │
│                                                                                         │
│  ┌───────────────────────────────┐              ┌─────────────────────────────────────┐ │
│  │ miair/airplay/server.py        │              │ miair/airplay/connections/          │ │
│  │ RTSP 服务端与状态机            │              │ AP2 多流连接、音频与控制管道        │ │
│  │ (OPTIONS/ANNOUNCE/SETUP/...)  │              │ - audio.py (ChaCha20 / RFC2198)     │ │
│  └──────────────┬────────────────┘              │ - control.py (RTCP / NTP / PTP)     │ │
│                 │                               │ - stream.py (Stream 96 / 103)       │ │
│                 ▼                               └──────────────────┬──────────────────┘ │
│  ┌───────────────────────────────┐                                 │                    │
│  │ JitterBuffer & 音频解码        │◄────────────────────────────────┘                    │
│  │ ALAC / AAC / PCM 解码为原始 PCM│                                                     │
│  └──────────────┬────────────────┘                                                      │
│                 ▼                                                                       │
│  ┌───────────────────────────────┐              ┌─────────────────────────────────────┐ │
│  │ miair/airplay/audio_stream.py │              │ miair/speaker.py                    │ │
│  │ HTTP 音频流服务               ├─────────────►│ 小爱音箱控制器 (SpeakerController)  │ │
│  │ (提供 stream.wav / stream.mp3)│  HTTP 拉流   │ - mina_service.play_by_url()        │ │
│  └───────────────────────────────┘              │ - 状态轮询、打断检测与自动续播      │ │
│                                                 └──────────────────┬──────────────────┘ │
└────────────────────────────────────────────────────────────────────┼────────────────────┘
                                                                     ▼
                                                  ┌─────────────────────────────────────┐
                                                  │         真实小米小爱音箱硬件        │
                                                  │ (Sound Pro / Play 增强版 / 等)      │
                                                  └─────────────────────────────────────┘
```

### 核心模块索引

- [`miair/airplay/server.py`](file:///Users/ray/sources/MiAir/miair/airplay/server.py): RTSP 会话核心、状态机与报文解析（`OPTIONS`, `ANNOUNCE`, `SETUP`, `RECORD`, `FLUSH`, `TEARDOWN`）。
- [`miair/airplay/mdns.py`](file:///Users/ray/sources/MiAir/miair/airplay/mdns.py): Bonjour / mDNS 服务发布器，管理 `_airplay._tcp` 和 `_raop._tcp` 广播与 TXT 属性。
- [`miair/airplay/bitflags.py`](file:///Users/ray/sources/MiAir/miair/airplay/bitflags.py): AirPlay 2 功能位 `FeatureFlags` 与状态位 `StatusFlags` 定义。
- [`miair/airplay/connections/`](file:///Users/ray/sources/MiAir/miair/airplay/connections): AirPlay 2 多流处理连接层：
  - [`audio.py`](file:///Users/ray/sources/MiAir/miair/airplay/connections/audio.py): RTP 音频解包、ChaCha20-Poly1305 解密、RFC 2198 冗余帧处理。
  - [`control.py`](file:///Users/ray/sources/MiAir/miair/airplay/connections/control.py): RTCP 时钟同步（NTP `0xd4`、Rexmit `0xd5/0xd6`、PTP `0xd7`）。
  - [`stream.py`](file:///Users/ray/sources/MiAir/miair/airplay/connections/stream.py): Stream 模式抽象（实时流 `96` 与缓冲流 `103`）。
- [`miair/airplay/pairing/`](file:///Users/ray/sources/MiAir/miair/airplay/pairing): HomeKit Accessory Protocol (HAP) / Transient Pairing 握手与 SRP-6a 协议实现。
- [`miair/airplay/audio_stream.py`](file:///Users/ray/sources/MiAir/miair/airplay/audio_stream.py): 音频 HTTP 流服务器（将解码后的 PCM 音频打包为 HTTP WAV/MP3 流供小爱拉取）。
- [`miair/airplay/speaker_airplay.py`](file:///Users/ray/sources/MiAir/miair/airplay/speaker_airplay.py): 音箱与 AirPlay 服务胶水层，负责启动/停止、端口映射与播放回调。
- [`miair/speaker.py`](file:///Users/ray/sources/MiAir/miair/speaker.py): 小爱音箱客户端交互核心，负责与小米云端及设备交互。
- [`miair/web/`](file:///Users/ray/sources/MiAir/miair/web): Web UI 及控制台 API（支持账号密码、Cookie、Token 登录与音箱配置）。

---

## 2. AirPlay 2 适配深度指南

### 2.1 协议核心差异对比

| 特性 | AirPlay 1 (RAOP) | AirPlay 2 |
| :--- | :--- | :--- |
| **服务发现** | 仅 `_raop._tcp.local.` 广播 | 同时广播 `_airplay._tcp.local.` 与 `_raop._tcp.local.` |
| **设备认证** | RSA 私钥挑战响应 (`Apple-Challenge`) 或 FairPlay 1/3 | HAP (HomeKit) 配对 / Transient Pairing (无 MFi 芯片验证) |
| **时钟同步** | 简单 NTP 往返测量 (Timing Port UDP) | IEEE 1588-2008 PTP (高精度纳秒/微秒级) / PTP-over-UDP |
| **传输模式** | 仅实时流 (RTP 单播，缓冲仅 ~800ms-2s) | **实时流 (Type 96)** 与 **长缓冲流 (Type 103, 预缓冲几十秒)** |
| **音频编码** | ALAC 44.1kHz 16-bit, PCM | AAC-LC, ALAC, AAC-ELD, 支持多通道及更高比特率 |
| **多房间同步** | 不支持（只能单选音箱） | 支持在 iOS 控制中心多选音箱、同步播放、音量联动与立体声配对 |

### 2.2 适配路线图与关键技术点

1. **Phase 1: mDNS 广播升级 (Dual-Registration)**
   - 在 [`miair/airplay/mdns.py`](file:///Users/ray/sources/MiAir/miair/airplay/mdns.py) 中同时激活 `_airplay._tcp` 与 `_raop._tcp`。
   - 配置正确的 feature 掩码（引用 [`miair/airplay/bitflags.py`](file:///Users/ray/sources/MiAir/miair/airplay/bitflags.py) 中的 `GetDefaultAirplayTwoFlags()`）：
     - `Ft09AirPlayAudio`: 必需，声明为音频接收器。
     - `Ft40BufferedAudio`: 开启长缓冲音频模式（核心！）。
     - `Ft41_PTPClock` & `Ft47PeerManagement`: 开启 PTP 时钟同步。
     - `Ft46HomeKitPairing` & `Ft48TransientPairing`: 开启临时免 MFi 配对。
   - 补齐关键 TXT 记录：`pk` (32 字节 Ed25519 公钥), `pi` (UUID), `gid` (组 UUID), `model` (`AudioAccessory1,1` 或 `AirPort4,107`)。

2. **Phase 2: 配对握手 (Pair-Setup / Pair-Verify)**
   - 在 [`miair/airplay/server.py`](file:///Users/ray/sources/MiAir/miair/airplay/server.py) 中拦截 RTSP `POST /pair-setup` 与 `POST /pair-verify`。
   - 使用 [`miair/airplay/pairing/hap.py`](file:///Users/ray/sources/MiAir/miair/airplay/pairing/hap.py) 完成 SRP-6a 协商与 Curve25519 临时密钥派生。
   - 派生出的 ChaCha20-Poly1305 会话密钥用于加密后续的 RTSP 消息或控制通道。

3. **Phase 3: PTP 高精度时钟同步**
   - 实现 PTP 时钟同步器（监听 UDP 319 / 320 端口或解析 RTCP `TIME_ANNOUNCE_PTP` 0xd7 报文）。
   - 将接收到的 RTP 时间戳精确锚定到本地时钟基准，确保多房间播放相位对齐。

4. **Phase 4: Buffered Audio (Stream 103) 管道与小爱推流整合**
   - 客户端（如 Apple Music、网易云等）通过 RTSP `SETUP` 协商 Stream 103。
   - 在 Buffered 模式下，音频数据包含较长缓冲（几秒至数十秒）：
     - **极佳优势**: 小爱音箱通过 `mina_service.play_by_url()` 拉取 HTTP 流正是缓冲拉流模式！
     - 相比 AirPlay 1 的超短 RTP 实时抖动窗口，AirPlay 2 的 Buffered 模式与小爱音箱本身的 HTTP 拉流管道高度吻合，能彻底解决网络抖动引起的爆音和卡顿。

---

## 3. 开发环境与依赖

本项目基于 Python `>=3.10`（本机统一使用 Python 3.13）。

- **虚拟环境路径**: `/Users/ray/sources/MiAir/.venv`
- **Python 解释器**: `/Users/ray/sources/MiAir/.venv/bin/python`
- **包管理器**: `/Users/ray/sources/MiAir/.venv/bin/pip`

### 环境激活与依赖安装

```bash
# 激活虚拟环境 (可选)
source .venv/bin/activate

# 安装可编辑依赖
./.venv/bin/pip install -e .
```

核心依赖包括：
- `aiohttp`: Web API 与 HTTP 音频流服务
- `av` (PyAV): ALAC / AAC 音频编解码
- `pycryptodome`: AES, RSA, ChaCha20 加解密
- `zeroconf`: Bonjour / mDNS 服务注册
- `miservice-fork`: 小爱音箱与小米云端 API 交互

---

## 4. 自动化测试体系 (Agent Test Harness)

针对 AirPlay 协议开发与验证，仓库提供了完备的**零硬件模拟测试套件**，可以在无真实 iPhone 与无真实小爱音箱的纯本地环境下完成完整端到端测试。

### 4.1 测试工具清单

1. **端到端 RTSP/RTP 客户端模拟器**: [`tests/mock_airplay_sender.py`](file:///Users/ray/sources/MiAir/tests/mock_airplay_sender.py)
   - 自动拉起本地独立 `AirPlayServer`（动态分配空闲 RTSP 端口）。
   - 模拟发送 `OPTIONS`（带 `Apple-Challenge`）并验证服务端计算签名。
   - 模拟发送 `ANNOUNCE`（SDP 载荷）、`SETUP`（端口协商）与 `RECORD`。
   - 构造合法 RTP 音频数据包并通过 UDP 发送到协商出的 `server_port`。
   - 模拟发送 `TEARDOWN` 并验证播放会话与资源释放。
2. **协议基础单元测试**: [`tests/test_airplay_basics.py`](file:///Users/ray/sources/MiAir/tests/test_airplay_basics.py)
   - 覆盖 `FeatureFlags` / `StatusFlags` 位掩码正确性。
   - 覆盖 `JitterBuffer` 插入、按序 drain、gap 扫描与容量淘汰。
   - 覆盖 `AP1Security` RSA 挑战响应加密计算。
   - 覆盖 `AirPlayMDNS` 命名与服务格式化。
3. **局域网多网卡与 IP 选择测试**: [`tests/test_ip_selection.py`](file:///Users/ray/sources/MiAir/tests/test_ip_selection.py)
4. **媒体 Seek 与格式魔数测试**: [`tests/test_audio_seek.py`](file:///Users/ray/sources/MiAir/tests/test_audio_seek.py)

### 4.2 常用测试命令

```bash
# 1. 运行所有 unittest 测试用例
./.venv/bin/python -m unittest discover tests

# 2. 单独运行端到端模拟发送器测试
./.venv/bin/python -m unittest tests/mock_airplay_sender.py

# 3. 单独运行协议基础单元测试
./.venv/bin/python -m unittest tests/test_airplay_basics.py

# 4. 运行音频 Seek 与格式魔数测试
./.venv/bin/python tests/test_audio_seek.py

# 5. 本地启动服务进行人工或设备调试 (指定独立测试配置目录)
./.venv/bin/python miair.py --conf-path conf --web-port 8300 --dlna-port 8200 --verbose
```

### 4.3 网络与广播排障命令

```bash
# 检查本机广播出的 AirPlay 1 (RAOP) 服务
dns-sd -B _raop._tcp .

# 检查本机广播出的 AirPlay 2 服务
dns-sd -B _airplay._tcp .

# 监听本地 RTSP 报文通信 (例如端口 5000 或测试端口)
sudo tcpdump -i lo0 -n -s 0 -A "tcp port 5000"
```

---

## 5. 开发纪律与避坑守则

1. **凭据安全**:
   - 严禁将真实小米账号、密码、passToken、serviceToken 或 Cookie 写入代码或提交到 git。
   - 配置文件统一位于 `conf/config.json`（已在 `.gitignore` 排除）。
   - 保留 API 接口返回时的 Cookie 与设备脱敏逻辑（`_mask_cookie`, `_mask_devices`）。
2. **跨线程与异步调用安全**:
   - RTSP 客户端连接在独立的工作线程中处理（非 asyncio 主事件循环）。
   - 从 RTSP 线程调用异步方法或触发播放事件时，**必须**使用 `asyncio.run_coroutine_threadsafe(coro, loop)`，严禁直接 `await` 或在子线程直接操作 event loop。
3. **Socket 与生命周期管理**:
   - AirPlay 连接断开（包括正常 `TEARDOWN` 和客户端超时异常退出）必须在 `finally` 块中彻底关闭 RTP、RTCP control、timing UDP sockets，重置 `is_playing` 标志并触发 `_safe_call_on_play_stop()`。
   - 避免未释放的 socket 导致端口泄漏或下次推流连接被拒绝。
4. **代码变更验证**:
   - 任何改动提交前，务必运行 `./.venv/bin/python -m unittest discover tests` 与 `./.venv/bin/python -m unittest tests/mock_airplay_sender.py` 确保回归测试全部通过。
