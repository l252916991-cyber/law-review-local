from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.formparsers import MultiPartException

from .case_reports import router as case_report_router
from .config import config
from .db import init_db, process_ownership
from .human_tasks import router as human_task_router
from .logger import request_id as logger_request_id
from .members import router as member_router
from .review_jobs import router as review_job_router
from .review_jobs import shutdown_review_executor, start_review_executor
from .routes import agent_runs as agent_runs_routes
from .routes import bank as bank_routes
from .routes import cases as cases_routes
from .routes import chat as chat_routes
from .routes import documents as documents_routes
from .routes import evidence as evidence_routes
from .routes import exports as exports_routes
from .routes import system as system_routes
from .routes.common import probe_redis
from .security import AccessMiddleware, apply_security_headers
from .security import router as access_router
from .statutory_retrieval import corpus_status
from .tasks import reconcile_batch_dispatches

access_logger = logging.getLogger("law_review.access")


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.ready = False
    application.state.redis_available = False
    if os.getenv("LAW_REVIEW_JSON_LOGS", "0") == "1":
        from .logger import setup_logging
        setup_logging(os.getenv("LAW_REVIEW_LOG_LEVEL", "INFO"))
    with process_ownership():
        init_db(seed=True, recover_runs=True)
        start_review_executor()
        try:
            application.state.indexing_semaphore = asyncio.Semaphore(max(1, config.indexing_concurrency))
            application.state.redis_available = await probe_redis()
            if application.state.redis_available:
                # Discharge batches whose enqueue outcome was never confirmed.
                dispatched = await reconcile_batch_dispatches()
                if dispatched:
                    logging.info("Redispatched %s pending batch import(s)", dispatched)
            # A broken corpus degrades statutory verification only; the case workspace
            # stays usable, so this never blocks startup. Readiness reports it.
            startup_corpus = corpus_status()
            if startup_corpus["configured"] and not startup_corpus["available"]:
                logging.error("Statutory corpus unavailable: %s", startup_corpus["error"])
            application.state.ready = True
            yield
        finally:
            application.state.ready = False
            application.state.redis_available = False
            await asyncio.to_thread(shutdown_review_executor)


app = FastAPI(
    title="LexVault 本地阅卷系统",
    description="基于视频功能拆解实现的私有化法律阅卷原型",
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(AccessMiddleware)
app.include_router(access_router)
app.include_router(review_job_router)
app.include_router(member_router)
app.include_router(human_task_router)
app.include_router(case_report_router)
app.include_router(system_routes.router)
app.include_router(cases_routes.router)
app.include_router(documents_routes.router)
app.include_router(evidence_routes.router)
app.include_router(chat_routes.router)
app.include_router(agent_runs_routes.router)
app.include_router(bank_routes.router)
app.include_router(exports_routes.router)


class RequestCorrelationMiddleware:
    """Attach a request id to every response and emit one access log line.

    A client-supplied id is echoed only when it looks sane; otherwise a fresh
    id is generated so hostile header values cannot pollute logs.
    """

    MAX_HEADER_LENGTH = 80

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore").strip()
        identifier = raw if 8 <= len(raw) <= self.MAX_HEADER_LENGTH and re.fullmatch(r"[\w.-]+", raw) else uuid.uuid4().hex
        token = logger_request_id.set(identifier)
        started = time.perf_counter()
        status_holder = {"status": 0}

        async def observing_send(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", identifier.encode("ascii"))]}
            await send(message)

        try:
            await self.app(scope, receive, observing_send)
        finally:
            if scope.get("path", "").startswith("/api/"):
                duration_ms = round((time.perf_counter() - started) * 1000)
                access_logger.info("%s %s", scope.get("method", ""), scope.get("path", ""),
                                   extra={"status": status_holder["status"], "duration_ms": duration_ms})
            logger_request_id.reset(token)


app.add_middleware(RequestCorrelationMiddleware)


class UploadBodyLimitMiddleware:
    """Bound multipart bytes before parsing, including chunked requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        is_upload = scope["type"] == "http" and scope.get("method") == "POST" and re.fullmatch(
            r"/api/cases/\d+/(documents|batch-import)", scope.get("path", "")
        )
        if not is_upload:
            return await self.app(scope, receive, send)
        # File bytes are checked exactly below; reserve a bounded header allowance.
        limit = config.max_upload_total_size + 1024 * 1024
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > limit:
            response = JSONResponse({"detail": "上传请求超过总大小限制"}, status_code=413)
            apply_security_headers(response, scope.get("path", ""))
            return await response(scope, receive, send)
        received = 0
        exceeded = False

        async def bounded_receive():
            nonlocal received, exceeded
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                exceeded = True
                # Starlette closes every spooled upload on this exception.
                raise MultiPartException("上传请求超过总大小限制")
            return message

        async def bounded_send(message):
            if exceeded and message["type"] == "http.response.start":
                message = {**message, "status": 413}
            await send(message)

        await self.app(scope, bounded_receive, bounded_send)


app.add_middleware(UploadBodyLimitMiddleware)


@app.get("/_next/{asset_path:path}", include_in_schema=False)
def next_frontend_asset(asset_path: str):
    from .frontend import frontend_asset

    return frontend_asset(asset_path)


@app.get("/", include_in_schema=False)
def index():
    from .frontend import frontend_shell

    return frontend_shell()


@app.get("/mobile", include_in_schema=False)
def mobile():
    from .frontend import frontend_shell

    return frontend_shell()

