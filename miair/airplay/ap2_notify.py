"""AirPlay 2 Shairport Sync 事件通知脚本

供 Shairport Sync 的 sessioncontrol 和 volume hooks 调用，将事件通过本地 UDP 发送回 Python Audio Bridge。
"""

import socket
import sys


def main():
    if len(sys.argv) < 3:
        return
    try:
        port = int(sys.argv[1])
        msg = " ".join(sys.argv[2:]).encode("utf-8")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.sendto(msg, ("127.0.0.1", port))
        s.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
