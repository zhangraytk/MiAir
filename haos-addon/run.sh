#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "          MiAir for Home Assistant (v1.1.1)               "
echo "=========================================================="

# 1. 确保持久化目录就绪
mkdir -p /data

# 2. 初始示例配置文件填充与 Home Assistant options.json 同步
python3 - << 'EOF'
import json
import os
import shutil

conf_dir = "/data"
conf_file = os.path.join(conf_dir, "config.json")
opts_file = "/data/options.json"
example_file = "/app/config-example.json"

if not os.path.exists(conf_file):
    if os.path.exists(example_file):
        shutil.copyfile(example_file, conf_file)
        print("[MiAir Add-on] Initialized /data/config.json from template.")
    else:
        with open(conf_file, "w", encoding="utf-8") as f:
            f.write("{}")

if os.path.exists(opts_file) and os.path.exists(conf_file):
    try:
        with open(opts_file, "r", encoding="utf-8") as f:
            opts = json.load(f)
        with open(conf_file, "r", encoding="utf-8") as f:
            conf = json.load(f)

        changed = False
        sync_keys = [
            "airplay_mode", "verbose", "dlna_port", "web_port",
            "account", "password", "mi_did", "hostname",
            "airplay2_port", "airplay2_prebuffer_ms", "airplay2_sync_debug"
        ]
        for k in sync_keys:
            if k in opts and opts[k] is not None and opts[k] != "":
                if conf.get(k) != opts[k]:
                    conf[k] = opts[k]
                    changed = True

        if changed:
            with open(conf_file, "w", encoding="utf-8") as f:
                json.dump(conf, f, indent=2, ensure_ascii=False)
            print("[MiAir Add-on] Synced options from Home Assistant into config.json.")
    except Exception as e:
        print(f"[MiAir Add-on] Notice: sync options exception: {e}")
EOF

# 3. 检查与配置 D-Bus 及 Avahi (mDNS 广播)
echo "[MiAir Add-on] 初始化独立 D-Bus 与 Avahi 广播守护..."
mkdir -p /run/dbus /var/run/avahi-daemon /var/run/dbus
rm -f /run/dbus/* /var/run/avahi-daemon/* /var/run/dbus/*
dbus-uuidgen --ensure=/etc/machine-id 2>/dev/null || true
dbus-daemon --system --fork 2>/dev/null || true
sleep 0.5
avahi-daemon -D --no-drop-root 2>/dev/null || true

# 4. 检查并启动 NQPTP (若安装)
if command -v nqptp >/dev/null 2>&1; then
    echo "[MiAir Add-on] 启动 NQPTP 时钟同步守护..."
    pkill nqptp 2>/dev/null || true
    nqptp -d 2>/dev/null || nqptp &
fi

# 5. 优雅退出信号处理 (SIGTERM/SIGINT)
child_pid=""
SHUTDOWN=0
stop_handler() {
    SHUTDOWN=1
    echo "[MiAir Add-on] 接收到终止信号，正在关闭服务..."
    if [ -n "$child_pid" ]; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
    pkill -TERM shairport-sync 2>/dev/null || true
    pkill -TERM nqptp 2>/dev/null || true
    exit 0
}
trap stop_handler SIGTERM SIGINT

# 6. 守护循环：当用户在 MiAir Web 界面保存并触发进程自重启时，自动重拉起
while [ "$SHUTDOWN" -eq 0 ]; do
    echo "[MiAir Add-on] 正在启动 MiAir 主程序..."
    python3 /app/miair.py --conf-path /data --verbose &
    child_pid=$!
    wait "$child_pid" || true
    status=$?
    if [ "$SHUTDOWN" -eq 1 ]; then
        break
    fi
    echo "[MiAir Add-on] MiAir 进程退出 (代码: $status)，2 秒后自动拉起..."
    sleep 2
done

