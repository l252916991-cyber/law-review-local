#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ ! -f "${LAW_REVIEW_FRONTEND_DIR:-frontend/out}/index.html" ]]; then
  printf '%s\n' '缺少前端构建。请先在 frontend/ 使用 Node .node-version 指定版本执行 pnpm install --frozen-lockfile 和 pnpm build:static。' >&2
  exit 1
fi
if command -v uv >/dev/null 2>&1; then
  exec uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8765
elif [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
else
  printf '%s\n' "缺少 uv 或已同步的 .venv；请先执行 uv sync --locked" >&2
  exit 1
fi
