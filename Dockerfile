# ============================================================
# Stage 1: 构建 NQPTP 与 Shairport Sync (支持 AirPlay 2)
# ============================================================
FROM debian:bookworm-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    autoconf \
    automake \
    libtool \
    pkg-config \
    libpopt-dev \
    libconfig-dev \
    libssl-dev \
    libsoxr-dev \
    libplist-dev \
    libsodium-dev \
    libgcrypt20-dev \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \
    uuid-dev \
    libavahi-client-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 1. 构建 NQPTP (AirPlay 2 高精度 PTP 时钟同步守护进程)
WORKDIR /src
RUN git clone --depth 1 https://github.com/mikebrady/nqptp.git \
    && cd nqptp \
    && autoreconf -fi \
    && ./configure \
    && make -j"$(nproc)" \
    && make install

# 2. 构建 Shairport Sync (启用 AirPlay 2、pipe 与 stdout 后端)
RUN git clone --depth 1 https://github.com/mikebrady/shairport-sync.git \
    && cd shairport-sync \
    && autoreconf -fi \
    && ./configure --sysconfdir=/etc \
        --with-pipe \
        --with-stdout \
        --with-avahi \
        --with-ssl=openssl \
        --with-soxr \
        --with-airplay-2 \
    && make -j"$(nproc)" \
    && make install

# ============================================================
# Stage 2: 运行时环境 (包含 Python 3.12 及 MiAir 核心服务)
# ============================================================
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

LABEL maintainer="MiAir"
LABEL description="DLNA/AirPlay (AirPlay 1 & Experimental AirPlay 2) receiver for Xiaomi AI Speaker"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libportaudio2 \
    libpopt0 \
    libconfig9 \
    libssl3 \
    libsoxr0 \
    libplist3 \
    libsodium23 \
    libgcrypt20 \
    libavahi-client3 \
    libavahi-common3 \
    avahi-daemon \
    avahi-utils \
    dbus \
    procps \
    net-tools \
    curl \
    dnsutils \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制 NQPTP 与 Shairport Sync
COPY --from=builder /usr/local/bin/nqptp /usr/local/bin/nqptp
COPY --from=builder /usr/local/bin/shairport-sync /usr/local/bin/shairport-sync

WORKDIR /app

# 安装 Python 依赖
COPY pyproject.toml .
RUN pip install --no-cache-dir . --root-user-action=ignore

# 拷贝 MiAir 核心代码与配置模板
COPY config-example.json .env.example ./
COPY miair.py ./
COPY miair/ ./miair/

# 拷贝启动脚本
COPY haos-addon/run.sh /run.sh
RUN chmod +x /run.sh

RUN mkdir -p /app/conf

EXPOSE 8200 8300 7000 5353/udp 1900/udp 319/udp 320/udp

ENTRYPOINT ["/run.sh"]
