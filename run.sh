#!/usr/bin/env bash
# 启动离线填报接收服务（纯 Python 标准库，无需安装依赖）
set -euo pipefail
cd "$(dirname "$0")"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"
export DATA_DIR="${DATA_DIR:-$(pwd)/data}"
export LEASE_SECONDS="${LEASE_SECONDS:-900}"
export GRACE_SECONDS="${GRACE_SECONDS:-1800}"
export REAPER_INTERVAL="${REAPER_INTERVAL:-30}"
exec python3 -m server.app
