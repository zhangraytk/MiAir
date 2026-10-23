# MiAir - Home Assistant Add-on

让小米小爱音箱支持 **DLNA** 与 **AirPlay**（含实验性 **AirPlay 2**）的高品质网络流媒体播放。

## 特性亮点

- **AirPlay 2 实验支持**: 基于 Shairport Sync 与 NQPTP 引擎实现的 AirPlay 2 协议桥接。
- **自动回退与共存**: 默认采用纯 Python 实现的 AirPlay 1 传统模式，实验模式失败或依赖缺失时自动平滑回退，不影响正常播放。
- **原生小爱推流**: 自动将接收到的 PCM 音频封装为 HTTP WAV/MP3 流并通过小米云端/米家协议通知音箱拉流。
- **Web UI 控制台**: 保留原生 Web 管理页面（默认端口 `8300`），支持扫码/Cookie/账号密码配置音箱与设备。
- **持久化存储**: 配置自动保存在 HAOS 的 `/data/conf/config.json` 中，Add-on 升级与重启不丢失。

## 已知问题 (Known Issues)

- **多设备播放不同步**：小爱音箱底层通过 HTTP 拉取本地音频流播放，各音箱硬件及 WiFi 缓冲区存在时延差异。当在 iOS / macOS 控制中心同时勾选多个设备串流时，会出现明显的音频不同步与延迟偏差。单设备推流正常。

## 安装指南

### 方法 1: 通过 HAOS Add-on 商店安装（推荐）
1. 在 Home Assistant 中进入 **配置 (Settings)** -> **加载项 (Add-ons)** -> **加载项商店 (Add-on Store)**。
2. 点击右上角菜单 -> **存储库 (Repositories)** -> 添加本仓库 URL：`https://github.com/zhangraytk/MiAir`。
3. 刷新后在列表中找到 **MiAir**，点击 **安装 (Install)**。
4. 开启 **开机自启 (Start on boot)** 并点击 **启动 (Start)**。
5. 点击 **打开 Web UI (Open Web UI)** 或直接访问 `http://<HAOS_IP>:8300` 进行音箱绑定。

### 方法 2: 本地加载项安装 (Local Add-on)
1. 将本仓库的 `haos-addon` 文件夹复制到 Home Assistant 的 `/addons/local/miair` 目录下。
2. 在 Add-on 商店页面点击 **检查更新 (Check for updates)**。
3. 在本地加载项列表中找到 **MiAir** 并安装。

## 配置选项

在 Add-on 的 **配置 (Configuration)** 页面中可以调整：

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `airplay_mode` | list | `legacy` | `legacy` (AirPlay 1) 或 `experimental` (AirPlay 2) |
| `verbose` | bool | `true` | 是否输出详细日志 |
| `dlna_port` | int | `8200` | DLNA 服务端口 |
| `web_port` | int | `8300` | Web 管理控制台端口 |
