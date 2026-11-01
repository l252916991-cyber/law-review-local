"""Document upload, batch import, directory, page correction, download and search endpoints."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..audit_trail import record_field_changes
from ..config import config
from ..db import connect, now, record_security_event, sync_fts_index, transaction
from ..security import actor_name, require_case_access, require_permission
from ..services import (
    contained_path,
    get_document,
    index_upload,
    rowdict,
    safe_filename,
    search_pages,
    upload_error_message,
)
from ..tasks import batch_temp_dir, cleanup_batch_files, enqueue_batch_import
from .common import app_state, require_case

router = APIRouter()


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
    semaphore = getattr(app_state(), "indexing_semaphore", None)
    if semaphore is None:
        semaphore = app_state().indexing_semaphore = asyncio.Semaphore(max(1, config.indexing_concurrency))
    async with semaphore:
        def index_file():
            return index_upload(case_id, item["filename"], Path(item["stored_path"]).read_bytes(), item["mime_type"])
        work = asyncio.create_task(asyncio.to_thread(index_file))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            await work
            raise


class DirectoryUpdate(BaseModel):
    doc_type: str | None = Field(default=None, max_length=40)
    people: str | None = Field(default=None, max_length=200)
    date_range: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(default=None, max_length=40)
    expected_version: int | None = Field(default=None, ge=1)


class PageTextUpdate(BaseModel):
    text: str = Field(max_length=200_000)
    expected_version: int | None = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=300)


@router.get("/api/cases/{case_id}/documents")
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


@router.post("/api/cases/{case_id}/documents", status_code=201)
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


@router.post("/api/cases/{case_id}/batch-import", status_code=202)
async def batch_import_documents(case_id: int, files: list[UploadFile] = File(...)):
    """Stage bounded files, persist their manifest, then enqueue for recovery."""
    require_case(case_id)
    batch_id = None
    file_infos = []
    try:
        if not getattr(app_state(), "redis_available", False):
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


@router.get("/api/batch-imports/{batch_id}")
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


@router.patch("/api/documents/{document_id}/directory")
def update_directory(document_id: int, payload: DirectoryUpdate):
    """人工校准材料目录，并记录改动前后值。"""
    require_permission("edit")
    document = get_document(document_id)
    if not document:
        raise HTTPException(404, "文件不存在")
    require_case_access(document["case_id"])
    allowed = {"doc_type", "people", "date_range", "summary", "status"}
    values = {key: value for key, value in payload.model_dump(exclude_none=True).items() if key in allowed}
    if not values:
        return document
    if payload.expected_version is not None and payload.expected_version != document["version"]:
        raise HTTPException(409, f"材料目录已被他人修改（当前版本 {document['version']}），请刷新后重试")
    with transaction() as conn:
        assignments = ", ".join(f"{key} = ?" for key in values)
        cursor = conn.execute(
            f"UPDATE documents SET {assignments}, updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
            (*values.values(), now(), document_id, document["version"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(409, "材料目录已被他人修改，请刷新后重试")
        record_field_changes(
            conn, case_id=document["case_id"], action="校准目录", target=f"材料#{document_id}《{document['name']}》",
            actor=actor_name(), before={key: document[key] for key in values}, after=values,
            versions=(document["version"], document["version"] + 1),
        )
    sync_fts_index()
    return get_document(document_id)


def _text_fingerprint(value: str) -> str:
    """Audit a long page without copying it: length, hash prefix and opening."""
    preview = " ".join(value.strip().split())[:80]
    return f"{len(value)}字 sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]} 开头:{preview}"


@router.patch("/api/documents/{document_id}/pages/{page_no}")
def correct_page_text(document_id: int, page_no: int, payload: PageTextUpdate):
    """纠正单页原文，并让依赖该页的已确认证据退回待复核。

    OCR 错字会直接污染检索与证据引用，因此允许人工纠错。纠错与审批失效在
    同一事务内完成：引用仍能在新原文中找到的证据保留确认，找不到的（含没有
    引用可核对的）退回待复核并清除确认人。
    """
    require_permission("edit")
    document = get_document(document_id)
    if not document:
        raise HTTPException(404, "文件不存在")
    require_case_access(document["case_id"])
    if payload.expected_version is not None and payload.expected_version != document["version"]:
        raise HTTPException(409, f"材料已被他人修改（当前版本 {document['version']}），请刷新后重试")

    ts = now()
    invalidated: list[dict[str, Any]] = []
    changed = False
    new_version = document["version"]
    with transaction() as conn:
        # Serialize the version check with the page/evidence writes. Two
        # requests can both have read the same document before entering here.
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT version FROM documents WHERE id = ?", (document_id,)).fetchone()
        if current is None:
            raise HTTPException(404, "文件不存在")
        if current["version"] != document["version"]:
            raise HTTPException(409, "材料已被他人修改，请刷新后重试")
        page = conn.execute(
            "SELECT id, text FROM pages WHERE document_id = ? AND page_no = ?", (document_id, page_no)
        ).fetchone()
        if page is None:
            raise HTTPException(404, "页面不存在")
        if page["text"] != payload.text:
            changed = True
            new_version = document["version"] + 1
            cursor = conn.execute(
                "UPDATE documents SET version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
                (ts, document_id, document["version"]),
            )
            if cursor.rowcount != 1:
                raise HTTPException(409, "材料已被他人修改，请刷新后重试")
            conn.execute("UPDATE pages SET text = ? WHERE id = ?", (payload.text, page["id"]))
            record_field_changes(
                conn, case_id=document["case_id"], action="纠错原文",
                target=f"材料#{document_id}《{document['name']}》第{page_no}页", actor=actor_name(),
                before={"原文": _text_fingerprint(page["text"]), "说明": ""},
                after={"原文": _text_fingerprint(payload.text), "说明": payload.reason},
                versions=(document["version"], new_version),
            )
            affected = conn.execute(
                "SELECT id, title, quote FROM evidence WHERE source_document_id = ? AND status = '已确认' "
                "AND source_page_start <= ? AND source_page_end >= ?",
                (document_id, page_no, page_no),
            ).fetchall()
            for item in affected:
                quote = (item["quote"] or "").strip()
                if quote and quote in payload.text:
                    continue
                conn.execute(
                    "UPDATE evidence SET status = '待复核', approved_by = NULL, approved_at = NULL, "
                    "version = version + 1, updated_at = ? WHERE id = ?",
                    (ts, item["id"]),
                )
                conn.execute(
                    "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                    (document["case_id"], "审批失效",
                     f"证据#{item['id']}《{item['title']}》因《{document['name']}》第{page_no}页原文纠错后引用不再匹配，"
                     "状态 已确认→待复核，确认人已清除", ts),
                )
                invalidated.append({"id": item["id"], "title": item["title"]})
    return {
        "page": document_page(document_id, page_no),
        "changed": changed,
        "invalidated": invalidated,
        "version": new_version,
        "message": "原文已纠正" if changed else "原文未变化",
    }


@router.get("/api/documents/{document_id}/file")
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
            raise HTTPException(404, "文件不存在") from None
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


@router.get("/api/documents/{document_id}/pages/{page_no}")
def document_page(document_id: int, page_no: int):
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT p.*, d.name, d.doc_type, d.version AS document_version
            FROM pages p JOIN documents d ON d.id = p.document_id
            WHERE p.document_id = ? AND p.page_no = ?
            """,
            (document_id, page_no),
        ).fetchone()
        if not row:
            raise HTTPException(404, "页面不存在")
        return rowdict(row)
    finally:
        conn.close()


@router.get("/api/cases/{case_id}/search")
def search(case_id: int, q: str = Query(min_length=1, max_length=500)):
    require_case(case_id)
    return search_pages(case_id, q, 20)

