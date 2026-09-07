from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.formparsers import MultiPartException

from .config import config
from .db import (
    CONVERSATION_ARCHIVE_RETENTION_SECONDS,
    connect,
    get_db_path,
    init_db,
    now,
    process_ownership,
    purge_expired_conversations,
    record_security_event,
    sync_fts_index,
    transaction,
)
from .logger import request_id as logger_request_id
from .services import (
    ArchivedConversationError,
    auto_analyze_case,
    build_export,
    build_export_package,
    bank_transaction_graph,
    chat,
    contained_path,
    get_export_template,
    get_document,
    index_upload,
    list_bank_transactions,
    local_llm_available,
    persist_bank_transactions,
    persist_parsed_bank_rows,
    rowdict,
    safe_filename,
    parse_spreadsheet,
    search_pages,
    summarize_bank_transactions,
    upload_error_message,
)
from .security import actor_name, allowed_case_ids, require_case_access, require_permission
from .security import AccessMiddleware, apply_security_headers, identity, router as access_router
from .review_jobs import router as review_job_router, shutdown_review_executor, start_review_executor
from .tasks import batch_temp_dir, cleanup_batch_files, enqueue_batch_import, get_redis_pool, reconcile_batch_dispatches


STATIC_DIR = Path(__file__).resolve().parent / "static"
access_logger = logging.getLogger("law_review.access")


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


async def _stage_upload(upload: UploadFile, destination: Path, total: int) -> tuple[int, int]:
    """Read bounded chunks, never materialize a complete request in memory."""
    size = 0
    with destination.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            total += len(chunk)
            if size > config.max_upload_size:
                raise HTTPException(413, f"文件 {upload.filename} 超过单文件大小限制")
            if total > config.max_upload_total_size:
                raise HTTPException(413, "上传文件合计超过总大小限制")
            await asyncio.to_thread(handle.write, chunk)
    return size, total


async def _index_staged_upload(case_id: int, item: dict[str, Any]) -> dict[str, Any]:
    semaphore = getattr(app.state, "indexing_semaphore", None)
    if semaphore is None:
        semaphore = app.state.indexing_semaphore = asyncio.Semaphore(max(1, config.indexing_concurrency))
    async with semaphore:
        def index_file():
            return index_upload(case_id, item["filename"], Path(item["stored_path"]).read_bytes(), item["mime_type"])
        work = asyncio.create_task(asyncio.to_thread(index_file))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            await work
            raise


class CaseCreate(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    case_no: str = ""
    case_type: str = "刑事"
    client_name: str = ""
    description: str = ""


class DirectoryUpdate(BaseModel):
    doc_type: str | None = None
    people: str | None = None
    date_range: str | None = None
    summary: str | None = None
    status: str | None = None


class EvidenceCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200, description="证据标题")
    category: str = Field(description="证据类别")
    fact: str = Field(min_length=1, description="待证事实")
    credibility: str = Field(default="待核验", description="可信度")
    status: str = Field(default="待复核", description="审核状态")
    source_document_id: int | None = Field(default=None, description="来源文档ID")
    source_page_start: int = Field(default=1, ge=1, description="起始页码")
    source_page_end: int = Field(default=1, ge=1, description="结束页码")
    quote: str = Field(default="", description="原文引用")

    @field_validator('category')
    @classmethod
    def validate_category(cls, v):
        valid = ['书证', '物证', '言词证据', '银行流水', '审计报告',
                 '询问笔录', '电子数据', '其他材料']
        if v not in valid:
            raise ValueError(f'类别必须是: {", ".join(valid)}')
        return v

    @field_validator('credibility')
    @classmethod
    def validate_credibility(cls, v):
        valid = ['待核验', '高', '较高', '中', '低']
        if v not in valid:
            raise ValueError(f'可信度必须是: {", ".join(valid)}')
        return v

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        valid = ['待复核', '已确认', '待质证', '待补证']
        if v not in valid:
            raise ValueError(f'状态必须是: {", ".join(valid)}')
        return v


class EvidenceUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    category: str | None = None
    fact: str | None = Field(default=None, min_length=1)
    credibility: str | None = None
    status: str | None = None
    source_document_id: int | None = None
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)
    quote: str | None = None

    @field_validator('category')
    @classmethod
    def validate_category(cls, v):
        if v is None:
            return v
        valid = ['书证', '物证', '言词证据', '银行流水', '审计报告',
                 '询问笔录', '电子数据', '其他材料']
        if v not in valid:
            raise ValueError(f'类别必须是: {", ".join(valid)}')
        return v

    @field_validator('credibility')
    @classmethod
    def validate_credibility(cls, v):
        if v is None:
            return v
        valid = ['待核验', '高', '较高', '中', '低']
        if v not in valid:
            raise ValueError(f'可信度必须是: {", ".join(valid)}')
        return v

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        if v is None:
            return v
        valid = ['待复核', '已确认', '待质证', '待补证']
        if v not in valid:
            raise ValueError(f'状态必须是: {", ".join(valid)}')
        return v


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    user_name: str = "本机律师"
    conversation_id: int | None = None
    use_llm: bool = True


class AnnotationCreate(BaseModel):
    user_name: str = Field(default="本机律师", min_length=1, max_length=100)
    annotation_type: Literal["备注", "疑点", "质证意见", "补证建议", "重点", "其他"] = "备注"
    content: str = Field(min_length=1, max_length=10000)
    quote_start: int | None = Field(default=None, ge=0)
    quote_end: int | None = Field(default=None, ge=0)
    status: Literal["待处理", "已处理"] = "待处理"


class AnnotationUpdate(BaseModel):
    annotation_type: Literal["备注", "疑点", "质证意见", "补证建议", "重点", "其他"] | None = None
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    quote_start: int | None = Field(default=None, ge=0)
    quote_end: int | None = Field(default=None, ge=0)
    status: Literal["待处理", "已处理"] | None = None


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


@app.get("/api/health")
def health():
    if not identity()["authenticated"]:
        return {"status": "ok"}
    available, model = local_llm_available()
    return {"status": "ok", "private_mode": True, "local_llm": available, "model": model}


@app.get("/api/benchmarks/lawbench")
def lawbench_dataset(
    task: str | None = Query(default=None),
    limit: int = Query(default=6, ge=0, le=200),
    seed: int = Query(default=42),
):
    from .lawbench import lawbench_summary

    try:
        return lawbench_summary(limit=limit, task_id=task, seed=seed)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/dashboard")
def dashboard():
    conn = connect()
    try:
        totals = {
            "cases": conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
            "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "pages": conn.execute("SELECT COALESCE(SUM(pages), 0) FROM documents").fetchone()[0],
            "evidence": conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        }
        recent = [
            rowdict(x)
            for x in conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT 8"
            ).fetchall()
        ]
        return {"totals": totals, "recent": recent}
    finally:
        conn.close()


@app.get("/api/cases")
def list_cases():
    conn = connect()
    try:
        permitted = allowed_case_ids()
        where = ""
        parameters: tuple = ()
        if permitted is not None:
            if not permitted:
                return []
            where = " WHERE c.id IN (" + ",".join("?" for _ in permitted) + ")"
            parameters = tuple(permitted)
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM documents d WHERE d.case_id = c.id) AS document_count,
                   (SELECT COALESCE(SUM(pages), 0) FROM documents d WHERE d.case_id = c.id) AS page_count,
                   (SELECT COUNT(*) FROM evidence e WHERE e.case_id = c.id) AS evidence_count
            FROM cases c
            """ + where + " ORDER BY c.updated_at DESC, c.id DESC", parameters,
        ).fetchall()
        return [rowdict(x) for x in rows]
    finally:
        conn.close()


@app.post("/api/cases", status_code=201)
def create_case(payload: CaseCreate):
    ts = now()
    with transaction() as conn:
        case_id = conn.execute(
            """
            INSERT INTO cases(title, case_no, case_type, client_name, status, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, '阅卷中', ?, ?, ?)
            """,
            (payload.title, payload.case_no, payload.case_type, payload.client_name, payload.description, ts, ts),
        ).lastrowid
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '创建案件', ?, ?)",
            (case_id, payload.title, ts),
        )
    return require_case(case_id)


@app.get("/api/cases/{case_id}")
def case_detail(case_id: int):
    case = require_case(case_id)
    conn = connect()
    try:
        case["metrics"] = {
            "documents": conn.execute("SELECT COUNT(*) FROM documents WHERE case_id = ?", (case_id,)).fetchone()[0],
            "pages": conn.execute("SELECT COALESCE(SUM(pages), 0) FROM documents WHERE case_id = ?", (case_id,)).fetchone()[0],
            "evidence": conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id = ?", (case_id,)).fetchone()[0],
            "confirmed": conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id = ? AND status = '已确认'", (case_id,)).fetchone()[0],
        }
        return case
    finally:
        conn.close()


@app.get("/api/cases/{case_id}/documents")
def list_documents(case_id: int):
    require_case(case_id)
    conn = connect()
    try:
        return [
            rowdict(x)
            for x in conn.execute("SELECT * FROM documents WHERE case_id = ? ORDER BY id", (case_id,)).fetchall()
        ]
    finally:
        conn.close()


@app.post("/api/cases/{case_id}/documents", status_code=201)
async def upload_documents(case_id: int, files: list[UploadFile] = File(...)):
    require_case(case_id)
    results = []
    failures = []
    try:
        if len(files) > config.max_files_per_upload:
            raise HTTPException(400, f"单次最多上传{config.max_files_per_upload}个文件")
        config.upload_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="request-", dir=config.upload_dir) as directory:
            items = []
            total = 0
            # Validate the entire aggregate before creating any indexed document.
            for index, upload in enumerate(files):
                path = Path(directory) / str(index)
                _, total = await _stage_upload(upload, path, total)
                items.append({"filename": safe_filename(upload.filename or "未命名"),
                              "stored_path": str(path), "mime_type": upload.content_type})
            for item in items:
                try:
                    results.append(await _index_staged_upload(case_id, item))
                except Exception as exc:
                    failures.append({"name": item["filename"], "error": upload_error_message(exc)})
    finally:
        for upload in files:
            await upload.close()
    return {"documents": results, "failures": failures}


@app.post("/api/cases/{case_id}/batch-import", status_code=202)
async def batch_import_documents(case_id: int, files: list[UploadFile] = File(...)):
    """Stage bounded files, persist their manifest, then enqueue for recovery."""
    require_case(case_id)
    batch_id = None
    file_infos = []
    try:
        if not getattr(app.state, "redis_available", False):
            raise HTTPException(503, "批量导入功能不可用：请启动 Redis 或使用普通上传。")
        if len(files) > config.batch_upload_limit:
            raise HTTPException(400, f"批量导入最多支持 {config.batch_upload_limit} 个文件")
        if not files:
            raise HTTPException(400, "未选择任何文件")
        with transaction() as conn:
            batch_id = conn.execute(
                "INSERT INTO batch_imports(case_id,total_files,status,created_at) VALUES (?,?,'queued',?)",
                (case_id, len(files), now()),
            ).lastrowid
        temp_dir = batch_temp_dir(batch_id)
        temp_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        for index, upload in enumerate(files):
            filename = safe_filename(upload.filename or f"file_{index}")
            file_key = uuid.uuid4().hex
            path = temp_dir / file_key
            _, total = await _stage_upload(upload, path, total)
            file_infos.append({"file_key": file_key, "filename": filename,
                               "stored_path": str(path),
                               "mime_type": upload.content_type or "application/octet-stream"})
        with transaction() as conn:
            for item in file_infos:
                conn.execute(
                    """INSERT INTO batch_import_files(batch_id,file_key,filename,stored_path,mime_type,status,error,updated_at)
                       VALUES (?,?,?,?,?,'pending','',?)""",
                    (batch_id, item["file_key"], item["filename"], item["stored_path"], item["mime_type"], now()),
                )
            # The dispatch outbox row commits atomically with the file manifest:
            # a crash before enqueue leaves a pending dispatch, not a lost batch.
            conn.execute(
                "INSERT INTO batch_dispatch(batch_id,case_id,state,attempts,created_at,updated_at) VALUES (?,?,'pending',0,?,?)",
                (batch_id, case_id, now(), now()),
            )
        job_id = await enqueue_batch_import(batch_id, case_id, file_infos)
        with transaction() as conn:
            conn.execute("UPDATE batch_dispatch SET state='discharged',updated_at=? WHERE batch_id=?", (now(), batch_id))
    except BaseException as exc:
        retained = False
        dispatch_pending = False
        if batch_id is not None:
            with transaction() as conn:
                # If Redis accepted the job but its acknowledgment was lost, a
                # worker may already own these files. Do not delete its inputs.
                batch = conn.execute("SELECT status FROM batch_imports WHERE id=?", (batch_id,)).fetchone()
                retained = bool(batch and batch["status"] != "queued")
                dispatch_pending = bool(
                    conn.execute(
                        "SELECT 1 FROM batch_dispatch WHERE batch_id=? AND state='pending'", (batch_id,)
                    ).fetchone()
                )
                if not retained and not dispatch_pending:
                    public_error = getattr(exc, "detail", "批量任务提交失败") if isinstance(exc, HTTPException) else f"批量任务提交失败（{type(exc).__name__}）"
                    conn.execute(
                        "UPDATE batch_imports SET status='failed',error_log_json=?,finished_at=? WHERE id=?",
                        (json.dumps([str(public_error)], ensure_ascii=False), now(), batch_id),
                    )
                    conn.execute(
                        "UPDATE batch_import_files SET status='failed',error=?,updated_at=? WHERE batch_id=? AND status='pending'",
                        ("上传或入队失败", now(), batch_id),
                    )
            if not retained and not dispatch_pending:
                cleanup_batch_files(batch_id)
        if isinstance(exc, HTTPException) or not isinstance(exc, Exception):
            raise
        logging.warning("Batch upload staging or enqueue failed (%s)", type(exc).__name__)
        if retained:
            return {"batch_id": batch_id, "job_id": f"batch-import-{batch_id}",
                    "total_files": len(files), "status": batch["status"],
                    "message": "任务已被接收；入队回执异常，请通过任务 ID 查询进度"}
        if dispatch_pending:
            return {"batch_id": batch_id, "job_id": f"batch-import-{batch_id}",
                    "total_files": len(files), "status": "queued",
                    "message": "任务已接收；入队未确认，队列恢复后将自动重新派发，请通过任务 ID 查询进度"}
        raise HTTPException(503, "批量任务提交失败，请检查队列服务后重试") from exc
    finally:
        for upload in files:
            await upload.close()

    return {
        "batch_id": batch_id,
        "job_id": job_id,
        "total_files": len(files),
        "status": "queued",
        "message": "批量导入任务已提交，请通过 GET /api/batch-imports/{batch_id} 查询进度"
    }


@app.get("/api/batch-imports/{batch_id}")
def get_batch_import_status(batch_id: int):
    """查询批量导入任务状态"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM batch_imports WHERE id=?", (batch_id,)
        ).fetchone()

        if not row:
            raise HTTPException(404, "批量导入任务不存在")

        batch = dict(row)
        batch["error_log"] = json.loads(batch.pop("error_log_json", "[]"))
        batch["progress_percent"] = (
            int(batch["processed_files"] * 100 / batch["total_files"])
            if batch["total_files"] > 0 else 0
        )

        return batch
    finally:
        conn.close()


@app.patch("/api/documents/{document_id}/directory")
def update_directory(document_id: int, payload: DirectoryUpdate):
    document = get_document(document_id)
    if not document:
        raise HTTPException(404, "文件不存在")
    values = payload.model_dump(exclude_none=True)
    if not values:
        return document
    allowed = {"doc_type", "people", "date_range", "summary", "status"}
    values = {k: v for k, v in values.items() if k in allowed}
    assignments = ", ".join(f"{key} = ?" for key in values)
    with transaction() as conn:
        conn.execute(
            f"UPDATE documents SET {assignments}, updated_at = ? WHERE id = ?",
            (*values.values(), now(), document_id),
        )
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '人工校准目录', ?, ?)",
            (document["case_id"], document["name"], now()),
        )
    sync_fts_index()
    return get_document(document_id)


@app.get("/api/documents/{document_id}/file")
def document_file(document_id: int, request: Request):
    document = get_document(document_id)
    if not document:
        raise HTTPException(404, "文件不存在")
    stored = Path(document["stored_path"]) if document["stored_path"] else None
    if stored and stored.exists() and stored.is_file():
        # Database metadata is not authorization: never serve files outside
        # the managed data directory, including through symlinks.
        try:
            stored = contained_path(stored, config.data_dir)
        except ValueError:
            raise HTTPException(404, "文件不存在")
        record_security_event("document_download", "success", actor=actor_name(),
                              case_id=document["case_id"], detail=f"document:{document_id}",
                              request_path=request.url.path)
        # Uploaded MIME is untrusted: never execute HTML/SVG at our session origin.
        suffix = stored.suffix.lower()
        safe_types = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".txt": "text/plain", ".md": "text/plain", ".csv": "text/plain", ".json": "text/plain", ".log": "text/plain"}
        media_type = safe_types.get(suffix, "application/octet-stream")
        return FileResponse(stored, media_type=media_type, filename=document["name"],
                            content_disposition_type="inline" if suffix in safe_types else "attachment",
                            headers={"Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff"})
    conn = connect()
    try:
        pages = conn.execute("SELECT page_no, text FROM pages WHERE document_id = ? ORDER BY page_no", (document_id,)).fetchall()
    finally:
        conn.close()
    text = "\n\n".join(f"===== 第{x['page_no']}页 =====\n{x['text']}" for x in pages)
    return PlainTextResponse(text, headers={"Content-Disposition": f'inline; filename="document-{document_id}.txt"'})


@app.get("/api/documents/{document_id}/pages/{page_no}")
def document_page(document_id: int, page_no: int):
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT p.*, d.name, d.doc_type FROM pages p JOIN documents d ON d.id = p.document_id
            WHERE p.document_id = ? AND p.page_no = ?
            """,
            (document_id, page_no),
        ).fetchone()
        if not row:
            raise HTTPException(404, "页面不存在")
        return rowdict(row)
    finally:
        conn.close()


@app.get("/api/cases/{case_id}/search")
def search(case_id: int, q: str = Query(min_length=1, max_length=500)):
    require_case(case_id)
    return search_pages(case_id, q, 20)


@app.get("/api/cases/{case_id}/evidence")
def list_evidence(case_id: int):
    require_case(case_id)
    conn = connect()
    try:
        evidence = [
            rowdict(x)
            for x in conn.execute(
                """
                SELECT e.*, d.name AS source_name FROM evidence e
                LEFT JOIN documents d ON d.id = e.source_document_id
                WHERE e.case_id = ? ORDER BY e.id
                """,
                (case_id,),
            )
        ]
        relations = [
            rowdict(x)
            for x in conn.execute(
                "SELECT * FROM evidence_relations WHERE case_id = ? ORDER BY id", (case_id,)
            )
        ]
        return {"evidence": evidence, "relations": relations}
    finally:
        conn.close()


@app.post("/api/cases/{case_id}/evidence", status_code=201)
def create_evidence(case_id: int, body: EvidenceCreate):
    """手动创建证据事项"""
    require_case(case_id)
    if body.status == "已确认":
        require_permission("approve")

    conn = connect()
    try:
        # 验证来源文档存在且属于该案件
        if body.source_document_id is not None:
            doc = conn.execute(
                "SELECT id FROM documents WHERE id = ? AND case_id = ?",
                (body.source_document_id, case_id)
            ).fetchone()
            if not doc:
                raise HTTPException(404, "来源文档不存在或不属于该案件")

        # 验证页码范围
        if body.source_page_end < body.source_page_start:
            raise HTTPException(400, "结束页码不能小于起始页码")

        cursor = conn.execute("""
            INSERT INTO evidence (
                case_id, title, category, fact, credibility, status,
                source_document_id, source_page_start, source_page_end,
                quote, approved_by, approved_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
        """, (
            case_id, body.title, body.category, body.fact, body.credibility,
            body.status, body.source_document_id, body.source_page_start,
            body.source_page_end, body.quote,
            actor_name() if body.status == "已确认" else None,
            now() if body.status == "已确认" else None
        ))

        evidence_id = cursor.lastrowid

        conn.commit()

        # 返回创建的证据
        row = conn.execute(
            """SELECT e.*, d.name AS source_name FROM evidence e
               LEFT JOIN documents d ON d.id = e.source_document_id
               WHERE e.id = ?""", (evidence_id,)
        ).fetchone()

        return {"evidence": rowdict(row), "message": "证据创建成功"}
    finally:
        conn.close()


@app.patch("/api/evidence/{evidence_id}")
def update_evidence(evidence_id: int, body: EvidenceUpdate):
    """更新证据事项"""
    conn = connect()
    try:
        # 验证证据存在
        evidence = conn.execute(
            "SELECT case_id, status, source_page_start, source_page_end FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if not evidence:
            raise HTTPException(404, "证据不存在")

        case_id = evidence["case_id"]

        # 验证来源文档（如果要更新）
        if body.source_document_id is not None:
            doc = conn.execute(
                "SELECT id FROM documents WHERE id = ? AND case_id = ?",
                (body.source_document_id, case_id)
            ).fetchone()
            if not doc:
                raise HTTPException(404, "来源文档不存在或不属于该案件")

        # 构建动态 UPDATE 语句
        updates = []
        params = []

        for field in ['title', 'category', 'fact', 'credibility', 'status',
                      'source_document_id', 'source_page_start',
                      'source_page_end', 'quote']:
            value = getattr(body, field)
            if value is not None:
                updates.append(f"{field} = ?")
                params.append(value)

        # Approval is attributed to the authenticated principal; moving an
        # item out of 已确认 clears the attribution. Model code has no write
        # path to this endpoint, so a confirmed status always has a human name.
        if body.status is not None:
            if body.status == "已确认":
                require_permission("approve")
                updates.append("approved_by = ?")
                params.append(actor_name())
                updates.append("approved_at = ?")
                params.append(now())
            else:
                updates.append("approved_by = NULL")
                updates.append("approved_at = NULL")

        # An approval binds the exact content that was confirmed. Editing the
        # content of a confirmed item without explicitly re-confirming voids
        # the old approval and returns the item to review.
        content_fields = ('title', 'category', 'fact', 'credibility',
                          'source_document_id', 'source_page_start', 'source_page_end', 'quote')
        if evidence["status"] == "已确认" and body.status != "已确认" and any(
            getattr(body, field) is not None for field in content_fields
        ):
            updates.append("status = '待复核'")
            updates.append("approved_by = NULL")
            updates.append("approved_at = NULL")

        if not updates:
            raise HTTPException(400, "至少需要更新一个字段")

        # 单独更新起始页或结束页时，也必须和数据库中的另一端联合校验。
        next_page_start = body.source_page_start if body.source_page_start is not None else evidence["source_page_start"]
        next_page_end = body.source_page_end if body.source_page_end is not None else evidence["source_page_end"]
        if next_page_end < next_page_start:
            raise HTTPException(400, "结束页码不能小于起始页码")

        params.append(evidence_id)
        sql = f"UPDATE evidence SET {', '.join(updates)} WHERE id = ?"

        conn.execute(sql, params)
        conn.commit()

        # 返回更新后的证据
        row = conn.execute(
            """SELECT e.*, d.name AS source_name FROM evidence e
               LEFT JOIN documents d ON d.id = e.source_document_id
               WHERE e.id = ?""", (evidence_id,)
        ).fetchone()

        return {"evidence": rowdict(row), "message": "证据更新成功"}
    finally:
        conn.close()


@app.delete("/api/evidence/{evidence_id}")
def delete_evidence(evidence_id: int):
    """删除证据事项（CASCADE 删除关联的标注和关系）"""
    conn = connect()
    try:
        # 验证证据存在并获取 case_id
        evidence = conn.execute(
            "SELECT id, title, case_id FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if not evidence:
            raise HTTPException(404, "证据不存在")

        # 获取关联统计（用于返回信息）
        annotations_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM evidence_annotations WHERE evidence_id = ?",
            (evidence_id,)
        ).fetchone()["cnt"]

        relations_count = conn.execute(
            """SELECT COUNT(*) as cnt FROM evidence_relations
               WHERE from_evidence_id = ? OR to_evidence_id = ?""",
            (evidence_id, evidence_id)
        ).fetchone()["cnt"]

        # 删除证据（CASCADE 会自动删除关联的 annotations 和 relations）
        conn.execute("DELETE FROM evidence WHERE id = ?", (evidence_id,))

        # 记录审计日志
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '删除证据', ?, ?)",
            (
                evidence["case_id"],
                f"证据ID:{evidence_id} 标题:《{evidence['title']}》 关联标注:{annotations_count} 关系:{relations_count}",
                now()
            )
        )

        conn.commit()

        return {
            "message": "证据删除成功",
            "deleted": {
                "evidence_id": evidence_id,
                "title": evidence["title"],
                "annotations_deleted": annotations_count,
                "relations_deleted": relations_count
            }
        }
    finally:
        conn.close()


def _validate_annotation_range(start: int | None, end: int | None, quote: str) -> None:
    if (start is None) != (end is None):
        raise HTTPException(400, "引用起止位置必须同时提供或同时为空")
    if start is not None and end is not None and (end <= start or end > len(quote)):
        raise HTTPException(400, "引用范围无效：结束位置须大于起始位置且不超过证据原文长度")


@app.get("/api/evidence/{evidence_id}/annotations")
def list_evidence_annotations(evidence_id: int):
    with transaction() as conn:
        evidence = conn.execute("SELECT case_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if evidence is None:
            raise HTTPException(404, "证据不存在")
        require_case_access(evidence["case_id"])
        return [dict(row) for row in conn.execute(
            "SELECT * FROM evidence_annotations WHERE evidence_id=? ORDER BY id", (evidence_id,),
        )]


@app.post("/api/evidence/{evidence_id}/annotations", status_code=201)
def create_evidence_annotation(evidence_id: int, body: AnnotationCreate):
    with transaction() as conn:
        evidence = conn.execute("SELECT case_id,quote FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if evidence is None:
            raise HTTPException(404, "证据不存在")
        require_case_access(evidence["case_id"])
        _validate_annotation_range(body.quote_start, body.quote_end, evidence["quote"] or "")
        annotation_id = conn.execute(
            """INSERT INTO evidence_annotations(evidence_id,user_name,annotation_type,content,quote_start,quote_end,status,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (evidence_id, actor_name(body.user_name), body.annotation_type, body.content,
             body.quote_start, body.quote_end, body.status, now()),
        ).lastrowid
        conn.execute(
            "INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'添加证据标注',?,?)",
            (evidence["case_id"], f"{actor_name(body.user_name)}：证据 {evidence_id} 标注 {annotation_id}", now()),
        )
        return dict(conn.execute("SELECT * FROM evidence_annotations WHERE id=?", (annotation_id,)).fetchone())


@app.patch("/api/evidence-annotations/{annotation_id}")
def update_evidence_annotation(annotation_id: int, body: AnnotationUpdate):
    with transaction() as conn:
        row = conn.execute(
            """SELECT a.*,e.case_id,e.quote FROM evidence_annotations a
               JOIN evidence e ON e.id=a.evidence_id WHERE a.id=?""", (annotation_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "标注不存在")
        require_case_access(row["case_id"])
        values = body.model_dump(exclude_unset=True)
        if not values:
            raise HTTPException(400, "至少需要更新一个字段")
        if any(value is None and key not in {"quote_start", "quote_end"} for key, value in values.items()):
            raise HTTPException(422, "标注类型、内容和状态不能为空")
        _validate_annotation_range(values.get("quote_start", row["quote_start"]),
                                   values.get("quote_end", row["quote_end"]), row["quote"] or "")
        assignments = ",".join(f"{key}=?" for key in values)
        conn.execute(f"UPDATE evidence_annotations SET {assignments} WHERE id=?", (*values.values(), annotation_id))
        conn.execute(
            "INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'更新证据标注',?,?)",
            (row["case_id"], f"{actor_name()}：标注 {annotation_id}", now()),
        )
        return dict(conn.execute("SELECT * FROM evidence_annotations WHERE id=?", (annotation_id,)).fetchone())


@app.delete("/api/evidence-annotations/{annotation_id}")
def delete_evidence_annotation(annotation_id: int):
    with transaction() as conn:
        row = conn.execute(
            "SELECT e.case_id FROM evidence_annotations a JOIN evidence e ON e.id=a.evidence_id WHERE a.id=?",
            (annotation_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "标注不存在")
        require_case_access(row["case_id"])
        conn.execute("DELETE FROM evidence_annotations WHERE id=?", (annotation_id,))
        conn.execute(
            "INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'删除证据标注',?,?)",
            (row["case_id"], f"{actor_name()}：标注 {annotation_id}", now()),
        )
        return {"message": "标注删除成功", "annotation_id": annotation_id}


@app.get("/api/cases/{case_id}/gap-analysis")
def get_gap_analysis(case_id: int, severity: str = Query(None)):
    """
    获取案件的证据疏漏检测结果

    参数:
        severity: 可选，按严重程度筛选 (高/中/低)
    """
    require_case(case_id)
    conn = connect()
    try:
        query = "SELECT * FROM gap_detections WHERE case_id = ?"
        params = [case_id]

        if severity:
            query += " AND severity = ?"
            params.append(severity)

        query += " ORDER BY CASE severity WHEN '高' THEN 1 WHEN '中' THEN 2 WHEN '低' THEN 3 END, id DESC"

        gaps = [rowdict(x) for x in conn.execute(query, params)]

        # 解析 affected_evidence_ids JSON 字段
        for gap in gaps:
            gap["affected_evidence_ids"] = json.loads(gap.get("affected_evidence_ids", "[]"))

        # 统计信息
        stats = {
            "total": len(gaps),
            "high": len([g for g in gaps if g["severity"] == "高"]),
            "medium": len([g for g in gaps if g["severity"] == "中"]),
            "low": len([g for g in gaps if g["severity"] == "低"]),
        }

        return {
            "gaps": gaps,
            "statistics": stats
        }
    finally:
        conn.close()


@app.post("/api/cases/{case_id}/analyze")
def analyze(case_id: int):
    require_case(case_id)
    return auto_analyze_case(case_id)


@app.get("/api/cases/{case_id}/conversations")
def list_conversations(case_id: int):
    require_case(case_id)
    with transaction() as cleanup_conn:
        purge_expired_conversations(cleanup_conn)
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c WHERE c.case_id = ? ORDER BY c.id DESC
            """,
            (case_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = rowdict(row)
            item["archived"] = item["archived_at"] is not None
            result.append(item)
        return result
    finally:
        conn.close()


@app.post("/api/conversations/{conversation_id}/archive")
def archive_conversation(conversation_id: int):
    archived_by = actor_name()
    expires_at = int(time.time()) + CONVERSATION_ARCHIVE_RETENTION_SECONDS
    with transaction() as conn:
        purge_expired_conversations(conn)
        conversation = conn.execute(
            "SELECT id, case_id, title, archived_at, archive_expires_at, archived_by FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()
        if not conversation:
            raise HTTPException(404, "会话不存在")
        if conversation["archived_at"] is None:
            archived_at = now()
            conn.execute(
                "UPDATE conversations SET archived_at=?, archive_expires_at=?, archived_by=? WHERE id=?",
                (archived_at, expires_at, archived_by, conversation_id),
            )
        else:
            archived_at = conversation["archived_at"]
            expires_at = conversation["archive_expires_at"]
            archived_by = conversation["archived_by"]
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '归档AI阅卷会话', ?, ?)",
            (conversation["case_id"], f"{archived_by}：会话#{conversation_id}（{conversation['title'][:60]}）", now()),
        )
    return {
        "conversation_id": conversation_id,
        "archived": True,
        "archived_at": archived_at,
        "archive_expires_at": expires_at,
        "archived_by": archived_by,
        "message": "会话已归档，将保留七天后自动清理",
    }


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: int):
    with transaction() as conn:
        purge_expired_conversations(conn)
        conversation = conn.execute("SELECT id, case_id, title FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not conversation:
            raise HTTPException(404, "会话不存在")
        message_count = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
        conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '永久删除AI阅卷会话', ?, ?)",
            (conversation["case_id"], f"{actor_name()}：会话#{conversation_id}（{conversation['title'][:60]}），消息{message_count}条", now()),
        )
    return {"conversation_id": conversation_id, "deleted": True, "messages_deleted": message_count, "message": "会话及其消息已永久删除，无法恢复"}


@app.get("/api/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: int):
    with transaction() as cleanup_conn:
        purge_expired_conversations(cleanup_conn)
    conn = connect()
    try:
        if conn.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,)).fetchone() is None:
            raise HTTPException(404, "会话不存在")
        rows = conn.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)).fetchall()
        result = []
        for row in rows:
            item = rowdict(row)
            item["citations"] = json.loads(item.pop("citations_json") or "[]")
            result.append(item)
        return result
    finally:
        conn.close()


@app.post("/api/cases/{case_id}/chat")
def case_chat(case_id: int, payload: ChatRequest):
    require_case(case_id)
    try:
        return chat(case_id, payload.question, actor_name(payload.user_name), payload.conversation_id, payload.use_llm)
    except ArchivedConversationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class AgentChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    user_name: str = "本机律师"
    mode: Literal["multi_agent", "langgraph"] = "multi_agent"
    use_llm: bool = True
    use_remote_embeddings: bool = True


@app.post("/api/cases/{case_id}/agent-chat")
def agent_chat(case_id: int, payload: AgentChatRequest):
    """Run the shared law-review agents on native DAG or LangGraph."""
    require_case(case_id)
    user_name = actor_name(payload.user_name)
    try:
        if payload.mode == "langgraph":
            from .langgraph_agents import create_langgraph_coordinator

            coordinator = create_langgraph_coordinator(case_id, payload.use_remote_embeddings)
        else:
            from .agents import create_coordinator

            coordinator = create_coordinator(case_id, payload.use_remote_embeddings)
        result = coordinator.process_query(payload.question, user_name, payload.use_llm)

        # 记录审计日志
        with transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, 'Agent 阅卷', ?, ?)",
                (case_id, f"{user_name}：{payload.question[:60]}", now()),
            )

        return {
            **result,
            "mode": payload.mode,
            "user_name": user_name,
        }
    except ImportError as exc:
        raise HTTPException(503, "Agent 运行依赖不可用；请按锁文件重新安装依赖") from exc
    except Exception as exc:
        if hasattr(exc, "run_id"):
            raise HTTPException(
                500,
                {
                    "message": "Agent 执行失败，请查看节点轨迹",
                    "run_id": exc.run_id,
                    "resumable": bool(getattr(exc, "resumable", False)),
                    "runtime": getattr(exc, "runtime", "langgraph"),
                    "checkpoint_thread_id": getattr(exc, "checkpoint_thread_id", ""),
                    "resume_count": getattr(exc, "resume_count", 0),
                },
            ) from exc
        raise HTTPException(500, "Agent 执行失败，请查看服务端诊断事件") from exc


@app.post("/api/agent-runs/{run_id}/resume")
def resume_agent_run(run_id: int):
    try:
        from .langgraph_agents import resume_langgraph_run

        result = resume_langgraph_run(run_id)
        with transaction() as conn:
            row = conn.execute("SELECT case_id FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if row:
                conn.execute("INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'恢复 LangGraph 运行',?,?)",
                             (row["case_id"], f"{actor_name()}：Run #{run_id}", now()))
        return result
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        if hasattr(exc, "run_id"):
            raise HTTPException(
                500,
                {
                    "message": "恢复失败，请查看节点轨迹",
                    "run_id": exc.run_id,
                    "resumable": bool(getattr(exc, "resumable", False)),
                    "runtime": getattr(exc, "runtime", "langgraph"),
                    "checkpoint_thread_id": getattr(exc, "checkpoint_thread_id", ""),
                    "resume_count": getattr(exc, "resume_count", 0),
                },
            ) from exc
        raise HTTPException(500, "恢复失败，请查看服务端诊断事件") from exc


@app.post("/api/cases/{case_id}/agent-compare")
def compare_agent_runtimes(case_id: int, payload: AgentChatRequest):
    require_case(case_id)
    user_name = actor_name(payload.user_name)
    try:
        from .agents import create_coordinator
        from .langgraph_agents import create_langgraph_coordinator
        from .runtime_comparison import compare_runtime_results
        from .rag import recall_memories

        memory_snapshot = recall_memories(case_id, payload.question, 3, payload.use_remote_embeddings)
        native = create_coordinator(case_id, payload.use_remote_embeddings).process_query(
            payload.question, user_name, payload.use_llm,
            persist_memory=False, memory_snapshot=memory_snapshot,
        )
        langgraph = create_langgraph_coordinator(
            case_id, payload.use_remote_embeddings
        ).process_query(
            payload.question, user_name, payload.use_llm,
            persist_memory=False, memory_snapshot=memory_snapshot,
        )
        comparison = compare_runtime_results(native, langgraph)
        with transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, 'Agent 双运行时对比', ?, ?)",
                (case_id, f"{user_name}：{payload.question[:60]}", now()),
            )
        return {
            "question": payload.question,
            "native": native,
            "langgraph": langgraph,
            "comparison": comparison,
        }
    except Exception as exc:
        if hasattr(exc, "run_id"):
            raise HTTPException(
                500,
                {
                    "message": "双运行时对比失败，请查看节点轨迹",
                    "run_id": exc.run_id,
                    "resumable": bool(getattr(exc, "resumable", False)),
                    "runtime": getattr(exc, "runtime", "langgraph"),
                    "checkpoint_thread_id": getattr(exc, "checkpoint_thread_id", ""),
                    "resume_count": getattr(exc, "resume_count", 0),
                },
            ) from exc
        raise HTTPException(500, "双运行时对比失败，请查看服务端诊断事件") from exc


@app.post("/api/cases/{case_id}/vector-index")
def build_vector_index(case_id: int):
    """
    为案件构建向量索引

    展示向量检索能力（Agent 核心组件）
    """
    require_case(case_id)
    try:
        from .rag import HybridRetriever

        tool = HybridRetriever(case_id, prefer_remote_embeddings=True)
        index = tool.ensure_vector_index(force=False)
        test_results, metrics = tool.retrieve("资金流向", limit=3)

        with transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '构建向量索引', ?, ?)",
                (case_id, f"已索引，测试检索返回 {len(test_results)} 条结果", now()),
            )

        return {
            "status": "success",
            "indexed": index,
            "test_results_count": len(test_results),
            "retrieval_metrics": metrics,
        }
    except ImportError as exc:
        raise HTTPException(503, "向量检索依赖不可用；请按锁文件重新安装依赖") from exc
    except Exception as exc:
        raise HTTPException(500, "索引失败，请查看服务端诊断事件") from exc


@app.get("/api/agent-runs/{run_id}")
def agent_run_trace(run_id: int):
    from .agents import get_run_trace

    trace = get_run_trace(run_id)
    if not trace:
        raise HTTPException(404, "Agent 运行记录不存在")
    return trace


@app.post("/api/cases/{case_id}/evaluate-rag")
def evaluate_rag(case_id: int, ground_truth: list[dict[str, Any]] | None = Body(default=None)):
    require_case(case_id)
    from .evaluation import evaluate_case

    try:
        return evaluate_case(case_id, prefer_remote_embeddings=True, k=5, ground_truth=ground_truth)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/cases/{case_id}/platform-metrics")
def case_platform_metrics(case_id: int):
    require_case(case_id)
    from .evaluation import platform_metrics

    return platform_metrics(case_id)


@app.post("/api/cases/{case_id}/bank-transactions/import", status_code=201)
async def import_bank_transactions(case_id: int, file: UploadFile = File(...)):
    require_case(case_id)
    if not (file.filename or "").lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(400, "流水文件仅支持 CSV、XLSX、XLS")
    payload = await file.read(config.max_upload_size + 1)
    await file.close()
    if len(payload) > config.max_upload_size:
        raise HTTPException(413, "流水文件超过单文件大小限制")
    try:
        result = index_upload(case_id, file.filename or "bank-transactions.csv", payload, file.content_type)
        source_hash = result.get("content_hash", "")
        if (file.filename or "").lower().endswith((".xlsx", ".xls")):
            rows = parse_spreadsheet(payload, file.filename or "")
            persisted = persist_bank_transactions(case_id, result["id"], source_hash, payload) if not rows else persist_parsed_bank_rows(case_id, result["id"], source_hash, rows)
        else:
            persisted = persist_bank_transactions(case_id, result["id"], source_hash, payload)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, upload_error_message(exc)) from exc
    record_security_event("bank_import", "success", actor=actor_name(), case_id=case_id,
                          detail=f"document:{result['id']} rows:{persisted['rows']}", request_path="/api/cases/%s/bank-transactions/import" % case_id)
    return {"document": result, "transactions": persisted}


@app.get("/api/cases/{case_id}/bank-transactions")
def get_bank_transactions(case_id: int, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), account: str | None = None, direction: Literal["inflow", "outflow", "unknown"] | None = None, counterparty: str | None = None, date_from: str | None = None, date_to: str | None = None):
    require_case(case_id)
    return list_bank_transactions(case_id, limit=limit, offset=offset, account=account, direction=direction, counterparty=counterparty, date_from=date_from, date_to=date_to)


@app.get("/api/cases/{case_id}/bank-transactions/summary")
def get_bank_transaction_summary(case_id: int, account: str | None = None, direction: Literal["inflow", "outflow", "unknown"] | None = None, counterparty: str | None = None, date_from: str | None = None, date_to: str | None = None):
    require_case(case_id)
    return summarize_bank_transactions(case_id, account=account, direction=direction, counterparty=counterparty, date_from=date_from, date_to=date_to)


@app.get("/api/cases/{case_id}/bank-transactions/graph")
def get_bank_transaction_graph(case_id: int):
    require_case(case_id)
    return bank_transaction_graph(case_id)


@app.get("/api/cases/{case_id}/export")
def export_case(case_id: int, request: Request, template_id: int | None = None, final: bool = False):
    require_case(case_id)
    try:
        path, package_sha = build_export_package(case_id, template_id=template_id, final=final, actor=actor_name())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    record_security_event("case_export", "success", actor=actor_name(), case_id=case_id,
                          detail=path.name, request_path=request.url.path)
    return FileResponse(path, media_type="application/zip", filename=path.name,
                        headers={"X-Package-Sha256": package_sha})


class ExportTemplateBlockIn(BaseModel):
    type: Literal["case_summary", "catalog_csv", "evidence_table", "qa_log", "static_markdown", "attachments"]
    title: str = Field(min_length=1, max_length=60)
    filename: str | None = Field(default=None, max_length=120)
    content: str = Field(default="", max_length=20000)
    evidence_status: Literal["待复核", "已确认", "待质证", "待补证"] | None = None
    required: bool = False


class ExportTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    blocks: list[ExportTemplateBlockIn] = Field(min_length=1, max_length=12)


def _reject_immutable_builtin(template: dict[str, Any] | None) -> dict[str, Any]:
    if template is None:
        raise HTTPException(404, "结案模板不存在")
    if template["builtin"]:
        raise HTTPException(409, "内置模板不可修改或删除")
    return template


@app.get("/api/export-templates")
def list_export_templates():
    conn = connect()
    try:
        return [
            {"id": row["id"], "name": row["name"], "description": row["description"],
             "builtin": bool(row["builtin"]), "updated_at": row["updated_at"]}
            for row in conn.execute("SELECT * FROM export_templates ORDER BY builtin DESC, id").fetchall()
        ]
    finally:
        conn.close()


@app.get("/api/export-templates/{template_id}")
def get_export_template_detail(template_id: int):
    require_permission("manage")
    template = get_export_template(template_id)
    if template is None:
        raise HTTPException(404, "结案模板不存在")
    return template


@app.post("/api/export-templates", status_code=201)
def create_export_template(body: ExportTemplateIn, request: Request):
    require_permission("manage")
    for block in body.blocks:
        if block.type == "static_markdown" and not block.content.strip():
            raise HTTPException(400, "静态说明区块必须填写内容")
        if block.type != "static_markdown" and block.type != "evidence_table" and block.evidence_status is not None:
            raise HTTPException(400, "仅证据表区块支持状态过滤")
    ts = now()
    with transaction() as conn:
        try:
            template_id = conn.execute(
                """INSERT INTO export_templates(name, description, blocks_json, builtin, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (body.name.strip(), body.description, json.dumps([b.model_dump() for b in body.blocks], ensure_ascii=False),
                 actor_name(), ts, ts),
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "同名结案模板已存在") from exc
    record_security_event("config", "export_template_created", actor=actor_name(), detail=body.name.strip(),
                          request_path=request.url.path)
    return {"id": template_id, "name": body.name.strip()}


@app.put("/api/export-templates/{template_id}")
def update_export_template(template_id: int, body: ExportTemplateIn, request: Request):
    require_permission("manage")
    _reject_immutable_builtin(get_export_template(template_id))
    for block in body.blocks:
        if block.type == "static_markdown" and not block.content.strip():
            raise HTTPException(400, "静态说明区块必须填写内容")
    with transaction() as conn:
        updated = conn.execute(
            "UPDATE export_templates SET name=?, description=?, blocks_json=?, updated_at=? WHERE id=? AND builtin=0",
            (body.name.strip(), body.description, json.dumps([b.model_dump() for b in body.blocks], ensure_ascii=False), now(), template_id),
        ).rowcount
    if not updated:
        raise HTTPException(404, "结案模板不存在")
    record_security_event("config", "export_template_updated", actor=actor_name(), detail=body.name.strip(),
                          request_path=request.url.path)
    return {"id": template_id, "name": body.name.strip()}


@app.delete("/api/export-templates/{template_id}")
def delete_export_template(template_id: int, request: Request):
    require_permission("manage")
    template = _reject_immutable_builtin(get_export_template(template_id))
    with transaction() as conn:
        conn.execute("DELETE FROM export_templates WHERE id=? AND builtin=0", (template_id,))
    record_security_event("config", "export_template_deleted", actor=actor_name(), detail=template["name"],
                          request_path=request.url.path)
    return {"deleted": True}


@app.get("/api/cases/{case_id}/audit")
def audit(case_id: int):
    require_case(case_id)
    conn = connect()
    try:
        return [
            rowdict(x)
            for x in conn.execute("SELECT * FROM audit_log WHERE case_id = ? ORDER BY id DESC LIMIT 100", (case_id,))
        ]
    finally:
        conn.close()


@app.get("/api/system/health")
async def system_health():
    """Readiness for core storage; Redis is an optional, freshly probed dependency.

    This is not a model/OCR/worker availability or writable-disk guarantee.
    """
    database_ok = False
    if getattr(app.state, "ready", False):
        def check_database():
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
    ready = database_ok and getattr(app.state, "ready", False)
    redis_ok = await probe_redis() if ready else False
    app.state.redis_available = redis_ok
    return JSONResponse({
        "status": "ok" if ready else "unavailable",
        "api": "healthy" if ready else "not_ready",
        "database": "available" if database_ok else "unavailable",
        "redis": "available" if redis_ok else "unavailable",
        "batch_import": "enabled" if redis_ok else "disabled",
    }, status_code=200 if ready else 503)


app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
