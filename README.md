# MiAir - 为小爱音箱添加 DLNA 与 AirPlay 支持

> [!NOTE]
> 本分支为 **AirPlay 2 实验性分支**（`feature/airplay2-experimental`）。

## 引用以下开源项目代码 由衷感谢

**[XiaoMusic](https://github.com/hanxi/xiaomusic "XiaoMusic")** &ensp; **[AirPlay2 Receiver](https://github.com/openairplay/airplay2-receiver "AirPlay2 Receiver")** &ensp; **[MaCast](https://github.com/xfangfang/Macast "MaCast")** &ensp; **[shairport-sync](https://github.com/mikebrady/shairport-sync "shairport-sync")**

## AirPlay 2 实验性支持说明

本分支引入了基于 Shairport Sync + NQPTP 的 AirPlay 2 实验性架构：

- **切换模式**：
  - **Web 管理界面**：在设置中将 AirPlay 模式选为 `AirPlay 2 (实验性)`。
  - **HAOS Add-on**：在配置项中将 `airplay_mode` 设为 `experimental`。
- **平滑回退**：若系统未编译/未安装 NQPTP 或 Shairport Sync，程序会自动回退到纯 Python 实现的 AirPlay 1 经典模式，保证基础播放可用。

### 已知问题 (Known Issues)
- **多设备播放不同步**：由于小爱音箱底层通过 HTTP 从本地音频服务拉取 WAV 数据流播放，不同音箱硬件解码与网络缓冲延迟存在差异，在苹果设备同时勾选多台小爱音箱播放时，设备之间会出现可感知的播放时差与不同步现象。单设备独立推流播放正常。

## 快速开始
## Windows
*确保设备已安装 Python 3.12+*

前往Release页面下载最新版本\
进入项目目录，使用终端执行
```python
python miair.py
```
程序将自动安装相关依赖库，请确保网络畅通\
安装完成后访问 `http://主机IP:8300` 即可打开 Web 管理界面。

## Docker (Thanks @SyunSS)

支持平台：Linux / OpenWrt / macOS

### 使用脚本部署
```bash
# 安装 Git
opkg update
opkg install git
opkg install git-http

# 克隆项目
rm -rf MiAir # 如果是更新，需要清理旧的部署目录
git clone https://github.com/KiriChen-Wind/MiAir.git
cd MiAir

# 赋予权限并运行安装脚本
chmod +x deploy.sh manage.sh
./deploy.sh
```

安装完成后访问 `http://容器宿主机IP:8300` 即可打开 Web 管理界面。
请确保容器网络为Host。\
请确保 Docker 容器重启策略设定为 Unless Stopped。\
部分情况下，修改配置后容器可能无法自动重启，请手动重启容器。


### Docker 相关命令
```bash
docker logs -f miair     # 查看日志
docker stop miair        # 停止
docker start miair       # 启动
docker restart miair     # 重启
```

## 我们
**[需要帮助&交流&测试版本发布](https://qun.qq.com/universal-share/share?ac=1&authKey=1zXhx2zxgw9GG2mkecypT9clD7q0B3W3l4K0D4fQirmpDWakz0Oy2BI3ocDrgzbh&busi_data=eyJncm91cENvZGUiOiI3NDEyNjcyOTgiLCJ0b2tlbiI6InYwbitXQTF5cE9MaUJCR0hMUk03OWV0WkFoMThxbjJRaWI4dHVlbUpGdW5OdEZBVEpXMXF0T1dQUnRmRXRzYVgiLCJ1aW4iOiIxODQxOTM4MDQwIn0%3D&data=_OrA-eASJMwYwx-Uj-BReC1Xh3zGAdkn8CQskbEsQ5S66bhqvvO6dJ-QrSlRl-Ks00l5XDw1FANE8Um0w5yB8Q&svctype=4&tempid=h5_group_info "需要帮助&交流&测试版本发布")**

## 后续 可能 添加的功能

- ~~支持 Docker 部署~~ ✅ 已支持
- 支持 OpenWrt 部署
- ~~支持 MacOS 部署~~
- ......


[![preview](https://raw.githubusercontent.com/KiriChen-Wind/MiAir/main/preview.png "preview")](https://raw.githubusercontent.com/KiriChen-Wind/MiAir/main/preview.png "preview")
