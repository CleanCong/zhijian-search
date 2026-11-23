#!/usr/bin/env bash
# 从 .env 读取配置后启动服务。
# 本地和服务器用同一个入口，避免「我这里能跑」的问题。
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "缺少 .env，请先复制 .env.example 并填入配置" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

exec python3 -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
