#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

echo "启动 LexVault 批量导入任务队列 worker..."
echo "Redis 配置：读取 REDIS_URL（不会回显凭证）"
echo ""

# 检查 Redis 是否可用
if command -v uv >/dev/null 2>&1; then
    RUNTIME=(uv run --locked python)
    WORKER=(uv run --locked arq)
elif [[ -x .venv/bin/python && -x .venv/bin/arq ]]; then
    RUNTIME=(.venv/bin/python)
    WORKER=(.venv/bin/arq)
else
    echo "缺少 uv 或已同步的 .venv；请先执行 uv sync --locked" >&2
    exit 1
fi

if ! "${RUNTIME[@]}" -c 'import os, redis; r = redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")); r.ping(); r.close()' 2>/dev/null; then
    echo "❌ Redis 服务不可用！"
    echo ""
    echo "请先启动 Redis："
    echo "  方式 1 (Docker): docker run -d -p 6379:6379 redis:7-alpine"
    echo "  方式 2 (Homebrew): brew services start redis"
    echo "  方式 3 (直接运行): redis-server"
    exit 1
fi

echo "✅ Redis 连接正常"
echo ""

# 启动 arq worker
exec "${WORKER[@]}" app.tasks.WorkerSettings
