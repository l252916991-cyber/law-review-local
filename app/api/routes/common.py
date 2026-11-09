"""Request models and helpers shared by several route modules."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from app.core.security import require_case_access
from app.documents.tasks import get_redis_pool
from app.storage.db import connect, rowdict


def app_state():
    """Application state of the running app (lazy import: main includes these routers)."""
    from app.main import app

    return app.state


async def probe_redis() -> bool:
    """Bound optional dependency checks and always release acquired pools."""
    pool = None
    try:
        async with asyncio.timeout(2):
            pool = await get_redis_pool()
            return bool(await pool.ping())
    except Exception as exc:
        logging.warning("Redis unavailable (%s); batch import disabled", type(exc).__name__)
        return False
    finally:
        if pool is not None:
            try:
                async with asyncio.timeout(2):
                    await pool.aclose()
            except Exception as exc:
                logging.warning("Redis pool close failed (%s)", type(exc).__name__)


def require_case(case_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(404, "案件不存在")
        require_case_access(case_id)
        return rowdict(row)
    finally:
        conn.close()


def model_operation_error(exc: Exception, fallback: str) -> HTTPException:
    code = str(exc).split(":", 1)[0]
    messages = {
        "model_unavailable_or_invalid": "生成模型暂不可用或返回内容未通过校验，请检查本地模型服务、可用内存及模型配置后重试。",
        "embedding_model_unavailable": "嵌入模型不可用，请检查本地模型服务及嵌入模型配置后重试。",
        "rerank_model_unavailable": "重排模型不可用，请检查本地模型服务及重排模型配置后重试。",
        "embedding_backend_changed_during_index": "嵌入模型在索引期间发生变化，请确认模型配置后重新构建索引。",
        "embedding_backend_changed_during_query": "嵌入模型与索引不一致，请重新构建索引。",
    }
    if code in messages:
        return HTTPException(503, {"code": code, "message": messages[code]})
    return HTTPException(500, fallback)

