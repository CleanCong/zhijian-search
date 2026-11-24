#!/usr/bin/env bash
# 知鉴 Search 后端生产启动脚本（任务 A3 · 进程守护底座）
#
# 前置（务必先读 backend/README.md「部署前必须验证」）：
#   本服务依赖本机知乎 CLI，凭据默认在 keychain。无头服务器上必须先解决：
#     export ZHIHU_CLI_PATH=/path/to/zhihu-cli
#     export ZHIHU_ACCESS_SECRET=xxx   # 去 https://developer.zhihu.com/profile 生成
#   DeepSeek（可选，缺省走规则抽取）：
#     export DEEPSEEK_API_KEY=xxx
#
# 密钥一律走环境变量，绝不写进任何被提交的文件（A3 要求）。
set -euo pipefail
cd "$(dirname "$0")/../backend"

HOST="${APP_HOST:-127.0.0.1}"
PORT="${APP_PORT:-8000}"
# 默认单 worker：多 worker = 多进程同时调知乎，容易撞限流（实测 4 路并发即 rate limit）。
# 演示场景单 worker + cache.py 缓存完全撑得住；确有并发需求再按额度谨慎上调。
WORKERS="${APP_WORKERS:-1}"

echo "[run_prod] host=$HOST port=$PORT workers=$WORKERS"
[ -n "${ZHIHU_ACCESS_SECRET:-}" ] && echo "[run_prod] ZHIHU_ACCESS_SECRET: 已配置" || echo "[run_prod] ZHIHU_ACCESS_SECRET: 未配置（CLI 在无 keychain 服务器会调不通，将走 degraded）"
[ -n "${DEEPSEEK_API_KEY:-}" ] && echo "[run_prod] DEEPSEEK_API_KEY: 已配置" || echo "[run_prod] DEEPSEEK_API_KEY: 未配置（结论走规则抽取）"

exec uvicorn main:app --host "$HOST" --port "$PORT" --workers "$WORKERS" --log-level info --no-access-log
