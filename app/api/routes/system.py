"""Health, readiness, model configuration, dashboard and benchmark dataset endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import urllib.parse
import urllib.request
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.routes.common import app_state, probe_redis
from app.cases.case_walls import exclusion_clause, hidden_case_ids
from app.core.config import LLMConfig, clear_model_override, load_model_override, save_model_override
from app.core.security import actor_name, identity, require_permission
from app.retrieval.statutory_retrieval import corpus_status
from app.review.services import assert_model_endpoint_allowed, local_llm_available, rowdict
from app.storage.db import connect, database_url, get_db_path, record_security_event

router = APIRouter()


class ModelConfigTest(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)


class ModelConfigUpdate(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=200)


@router.get("/api/health")
def health():
    if not identity()["authenticated"]:
        return {"status": "ok"}
    available, model = local_llm_available()
    return {"status": "ok", "private_mode": True, "local_llm": available, "model": model,
            "corpus": corpus_status()}


@router.get("/api/ready")
def ready():
    """Readiness probe: legal verification is unavailable unless the corpus loaded intact.

    Reports only availability, never the corpus path, so it can stay unauthenticated.
    """
    status = corpus_status()
    return {"status": "ok" if status["available"] else "degraded",
            "legal_verification": "available" if status["available"] else "unavailable"}


@router.get("/api/model-config")
def read_model_config():
    """Effective model endpoint and chat model, so the UI can prefill its form."""
    llm = LLMConfig.load()
    return {"base_url": llm.base_url, "model": llm.model, "overridden": bool(load_model_override())}


@router.put("/api/model-config")
def update_model_config(payload: ModelConfigUpdate):
    """Persist an operator override for the local model endpoint and chat model.

    The endpoint must satisfy the same egress policy the model call sites enforce,
    so the UI cannot point case text at an unapproved destination.
    """
    base_url = payload.base_url.strip().rstrip("/")
    model = payload.model.strip()
    try:
        assert_model_endpoint_allowed(base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if any(ord(char) < 32 for char in model):
        raise HTTPException(400, "模型名称包含无效字符")
    save_model_override({"base_url": base_url, "model": model})
    record_security_event("config", "model_config_updated", actor=actor_name(),
                          detail=f"{base_url} · {model}", request_path="/api/model-config")
    return {"base_url": base_url, "model": model, "overridden": True}


@router.delete("/api/model-config")
def reset_model_config():
    """Drop the in-app override and fall back to the deployment environment."""
    clear_model_override()
    llm = LLMConfig.load()
    record_security_event("config", "model_config_reset", actor=actor_name(), request_path="/api/model-config")
    return {"base_url": llm.base_url, "model": llm.model, "overridden": False}


@router.post("/api/model-config/test")
def test_model_config(payload: ModelConfigTest):
    """Check an OpenAI-compatible local model service without exposing its response body."""
    require_permission("view")
    base_url = payload.base_url.rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(400, "模型服务地址无效") from exc
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(400, "模型服务地址必须是本机地址")
    connection_url = base_url
    loopback_host = os.getenv("LAW_REVIEW_MODEL_LOOPBACK_HOST", "").strip()
    if loopback_host:
        port_suffix = f":{port}" if port is not None else ""
        connection_url = urllib.parse.urlunsplit(
            (parsed.scheme, f"{loopback_host}{port_suffix}", parsed.path, parsed.query, parsed.fragment)
        )
    try:
        request = urllib.request.Request(f"{connection_url}/models", headers={"Accept": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            body = json.loads(response.read())
        models = [item.get("id") for item in body.get("data", []) if item.get("id")]
        return {"reachable": True, "models": models}
    except Exception as exc:
        raise HTTPException(502, f"无法连接本地模型服务（{type(exc).__name__}）") from exc


@router.get("/api/benchmarks/lawbench")
def lawbench_dataset(
    task: str | None = Query(default=None),
    limit: int = Query(default=6, ge=0, le=200),
    seed: int = Query(default=42),
):
    from app.benchmarks.lawbench import lawbench_summary

    try:
        return lawbench_summary(limit=limit, task_id=task, seed=seed)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/dashboard")
def dashboard():
    hidden = hidden_case_ids()
    conn = connect()
    try:
        def count(sql: str, column: str) -> int:
            clause, parameters = exclusion_clause(column, hidden)
            return conn.execute(sql + " WHERE 1=1" + clause, parameters).fetchone()[0]

        totals = {
            "cases": count("SELECT COUNT(*) FROM cases", "id"),
            "documents": count("SELECT COUNT(*) FROM documents", "case_id"),
            "pages": count("SELECT COALESCE(SUM(pages), 0) FROM documents", "case_id"),
            "evidence": count("SELECT COUNT(*) FROM evidence", "case_id"),
            "conversations": count("SELECT COUNT(*) FROM conversations", "case_id"),
        }
        # The audit log stays complete for compliance, but this feed shows
        # nothing from walls its reader is outside of.
        clause, parameters = exclusion_clause("case_id", hidden)
        recent = [
            rowdict(x)
            for x in conn.execute(
                "SELECT * FROM audit_log WHERE 1=1" + clause + " ORDER BY id DESC LIMIT 8", parameters
            ).fetchall()
        ]
        return {"totals": totals, "recent": recent}
    finally:
        conn.close()


@router.get("/api/system/health")
async def system_health():
    """Readiness for core storage; Redis is an optional, freshly probed dependency.

    This is not a model/OCR/worker availability or writable-disk guarantee.
    """
    database_ok = False
    if getattr(app_state(), "ready", False):
        def check_database():
            if database_url():
                with closing(connect()) as pg:
                    pg.execute("SELECT id FROM cases LIMIT 1").fetchone()
                return
            # Never create a missing database or alter journal mode during a probe.
            conn = sqlite3.connect(get_db_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                conn.execute("SELECT id FROM cases LIMIT 1").fetchone()
            finally:
                conn.close()
        try:
            await asyncio.to_thread(check_database)
            database_ok = True
        except Exception as exc:
            logging.warning("Readiness database check failed (%s)", type(exc).__name__)
    ready = database_ok and getattr(app_state(), "ready", False)
    redis_ok = await probe_redis() if ready else False
    app_state().redis_available = redis_ok
    return JSONResponse({
        "status": "ok" if ready else "unavailable",
        "api": "healthy" if ready else "not_ready",
        "database": "available" if database_ok else "unavailable",
        "redis": "available" if redis_ok else "unavailable",
        "batch_import": "enabled" if redis_ok else "disabled",
    }, status_code=200 if ready else 503)

