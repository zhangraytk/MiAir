#!/bin/sh
set -e

echo "=========================================================="
echo "          MiAir for Home Assistant OS (v1.1.0)           "
echo "=========================================================="

# 1. 确定配置存储路径 (优先 HAOS /data 目录，兼顾标准 Docker /app/conf)
if [ -d "/data" ] && [ -w "/data" ]; then
    DATA_DIR="/data"
    CONF_DIR="${DATA_DIR}/conf"
else
    DATA_DIR="/app"
    CONF_DIR="/app/conf"
fi

if [ ! -d "${CONF_DIR}" ]; then
    echo "[MiAir HAOS] 初始化配置持久化目录: ${CONF_DIR}"
    mkdir -p "${CONF_DIR}"
fi

# 初始示例配置文件填充
if [ ! -f "${CONF_DIR}/config.json" ]; then
    if [ -f "/app/config-example.json" ]; then
        echo "[MiAir HAOS] 首次启动，复制示例配置至持久化存储"
        cp /app/config-example.json "${CONF_DIR}/config.json"
    fi
fi

if [ ! -f "${CONF_DIR}/.env" ] && [ -f "/app/.env.example" ]; then
    cp /app/.env.example "${CONF_DIR}/.env"
fi

# 2. 同步 Home Assistant Add-on 选项 (options.json)
if [ -f "${DATA_DIR}/options.json" ]; then
    echo "[MiAir HAOS] 检测到 HAOS 选项，正在同步至配置..."
    python3 - <<EOF
import json, os

options_file = "${DATA_DIR}/options.json"
config_file = "${CONF_DIR}/config.json"

try:
    with open(options_file, "r", encoding="utf-8") as f:
        options = json.load(f)
except Exception as e:
    options = {}

config = {}
if os.path.exists(config_file):
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        config = {}

changed = False
for key in ["airplay_mode", "verbose", "dlna_port", "web_port"]:
    if key in options and options[key] is not None:
        if config.get(key) != options[key]:
            config[key] = options[key]
            changed = True

if changed or not os.path.exists(config_file):
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print("[MiAir HAOS] 已成功同步 HAOS 选项至 config.json")
EOF
fi

# 3. 检查与配置 D-Bus 及 Avahi (mDNS 广播)
echo "[MiAir HAOS] 正在检查 D-Bus 与 Avahi 广播环境..."
if [ -S "/run/dbus/system_bus_socket" ]; then
    echo "[MiAir HAOS] 检测到宿主机 D-Bus 接口 (/run/dbus/system_bus_socket)，使用宿主机 Avahi 服务"
else
    echo "[MiAir HAOS] 未挂载宿主机 D-Bus，正在容器内部启动独立 D-Bus 与 Avahi..."
    mkdir -p /run/dbus
    rm -f /run/dbus/pid /run/dbus/system_bus_socket
    dbus-daemon --system --fork 2>/dev/null || true
    
    mkdir -p /var/run/avahi-daemon
    rm -f /var/run/avahi-daemon/pid
    avahi-daemon -D --no-drop-root 2>/dev/null || true
fi

# 4. 端口与网络共存性诊断
echo "[MiAir HAOS] 正在执行网络端口与服务共存检测..."
python3 - <<EOF
import socket

def check_port(proto, port, desc):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        print(f"  - [{proto.upper()} {port}] 可正常绑定 ({desc})")
        return True
    except OSError as e:
        print(f"  ! [{proto.upper()} {port}] 已被占用或共享监听 ({desc}): {e}")
        return False
    finally:
        s.close()

check_port("udp", 5353, "mDNS / Bonjour 广播")
check_port("udp", 1900, "SSDP / DLNA 发现")
check_port("udp", 319, "NQPTP PTP Event 端口")
check_port("udp", 320, "NQPTP PTP General 端口")
check_port("tcp", 8300, "MiAir Web 管理界面")
check_port("tcp", 8200, "DLNA 服务端口")
EOF

# 5. 检查并启动 NQPTP (AirPlay 2 时钟同步守护进程)
if command -v nqptp >/dev/null 2>&1; then
    echo "[MiAir HAOS] 正在启动 NQPTP 时钟同步守护进程..."
    pkill nqptp 2>/dev/null || true
    nqptp -d 2>/dev/null || nqptp &
    sleep 0.5
    if pgrep nqptp >/dev/null 2>&1; then
        echo "[MiAir HAOS] NQPTP 启动成功 (PID: $(pgrep nqptp | head -n 1))"
    else
        echo "[MiAir HAOS] 警告: NQPTP 未能成功运行（若运行在经典模式不受影响）"
    fi
else
    echo "[MiAir HAOS] 系统未安装 nqptp 二进制，跳过 NQPTP 启动"
fi

# 6. 优雅退出信号捕获
MIAIR_PID=""
term_handler() {
    echo ""
    echo "[MiAir HAOS] 接收到终止信号 (SIGTERM/SIGINT)，开始优雅退出..."
    if [ -n "$MIAIR_PID" ]; then
        kill -TERM "$MIAIR_PID" 2>/dev/null || true
        wait "$MIAIR_PID" 2>/dev/null || true
    fi
    pkill -TERM shairport-sync 2>/dev/null || true
    pkill -TERM nqptp 2>/dev/null || true
    echo "[MiAir HAOS] 所有服务已清理，容器退出完成。"
    exit 0
}

trap 'term_handler' SIGTERM SIGINT

# 7. 启动 MiAir 主程序
echo "[MiAir HAOS] 启动 MiAir 主应用程序..."
python3 miair.py --conf-path "${CONF_DIR}" --verbose &
MIAIR_PID=$!

# 等待主进程
wait "$MIAIR_PID"
EXIT_CODE=$?
echo "[MiAir HAOS] MiAir 进程已退出，代码: ${EXIT_CODE}"
exit $EXIT_CODE
