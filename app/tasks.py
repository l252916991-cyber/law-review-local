"""Local arq batch ingestion with durable per-file recovery and bounded workers."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from .config import config
from .db import init_db, now, transaction
from .services import index_upload, upload_error_message

logger = logging.getLogger(__name__)
TERMINAL_BATCH_STATES = {"completed", "completed_with_errors", "failed"}


async def get_redis_pool() -> ArqRedis:
    """Create an explicitly owned pool; the caller must close it in finally."""
    return await create_pool(
        RedisSettings.from_dsn(config.redis.url),
        default_queue_name=config.redis.queue_name,
    )


def batch_temp_dir(batch_id: int) -> Path:
    if batch_id <= 0:
        raise ValueError("Invalid batch ID")
    return config.upload_dir / "batch_temp" / str(batch_id)


def cleanup_batch_files(batch_id: int) -> None:
    """Remove only this batch's staging files; never touch indexed documents."""
    directory = batch_temp_dir(batch_id)
    if not directory.exists() or directory.is_symlink():
        return
    for item in directory.iterdir():
        if item.is_file() or item.is_symlink():
            item.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        logger.warning("Batch %s staging directory has unexpected nested entries", batch_id)


def _batch_progress(batch_id: int, *, finalize: bool = False) -> dict[str, Any]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT filename,status,error FROM batch_import_files WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        successful = sum(row["status"] == "completed" for row in rows)
        failed = sum(row["status"] == "failed" for row in rows)
        errors = [f"{row['filename']}: {row['error']}" for row in rows if row["status"] == "failed"]
        conn.execute(
            "UPDATE batch_imports SET processed_files=?,successful_files=?,failed_files=?,error_log_json=? WHERE id=?",
            (successful + failed, successful, failed, json.dumps(errors, ensure_ascii=False), batch_id),
        )
        if finalize and successful + failed == len(rows):
            status = "completed" if not failed else "completed_with_errors" if successful else "failed"
            conn.execute("UPDATE batch_imports SET status=?,finished_at=? WHERE id=?", (status, now(), batch_id))
    return {"batch_id": batch_id, "successful": successful, "failed": failed, "errors": errors}


def _index_staged_file(batch_id: int, case_id: int, item: dict[str, Any]) -> dict[str, Any]:
    path = Path(item["stored_path"])
    directory = batch_temp_dir(batch_id).resolve()
    if path.is_symlink() or path.resolve().parent != directory:
        raise ValueError("Staging path is outside the batch directory")
    if path.stat().st_size > config.max_upload_size:
        raise ValueError("Staged file exceeds the upload size limit")
    return index_upload(
        case_id, item["filename"], path.read_bytes(), item["mime_type"],
        import_key=f"batch:{batch_id}:{item['file_key']}",
    )


async def process_batch_import(
    ctx: dict[str, Any], batch_id: int, case_id: int, files: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resume only unfinished files; document import keys close commit/replay gaps."""
    with transaction() as conn:
        batch = conn.execute("SELECT * FROM batch_imports WHERE id=? AND case_id=?", (batch_id, case_id)).fetchone()
        if batch is None:
            raise ValueError("Batch does not exist or belongs to another case")
        if batch["status"] in TERMINAL_BATCH_STATES:
            return {"batch_id": batch_id, "successful": batch["successful_files"],
                    "failed": batch["failed_files"], "errors": json.loads(batch["error_log_json"])}
        for index, item in enumerate(files):
            conn.execute(
                """INSERT OR IGNORE INTO batch_import_files
                   (batch_id,file_key,filename,stored_path,mime_type,status,error,updated_at)
                   VALUES (?,?,?,?,?,'pending','',?)""",
                (batch_id, str(item.get("file_key", index)), item["filename"], item["stored_path"],
                 item.get("mime_type", "application/octet-stream"), now()),
            )
        conn.execute("UPDATE batch_imports SET status='processing',started_at=COALESCE(started_at,?) WHERE id=?", (now(), batch_id))
        pending = [dict(row) for row in conn.execute(
            "SELECT * FROM batch_import_files WHERE batch_id=? AND status IN ('pending','processing') ORDER BY id",
            (batch_id,),
        )]

    semaphore = ctx.setdefault("indexing_semaphore", asyncio.Semaphore(max(1, config.indexing_concurrency)))

    async def process_one(item: dict[str, Any]) -> None:
        async with semaphore:
            with transaction() as conn:
                conn.execute("UPDATE batch_import_files SET status='processing',updated_at=? WHERE id=?", (now(), item["id"]))
            cancelled = False
            document_id = None
            error = ""
            try:
                # An arq timeout must not replay a file while its native thread
                # is still committing. Wait for that bounded file operation.
                work = asyncio.create_task(asyncio.to_thread(_index_staged_file, batch_id, case_id, item))
                try:
                    document = await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled = True
                    document = await work
                document_id = document["id"]
                status = "completed"
            except Exception as exc:
                status = "failed"
                error = upload_error_message(exc)
                logger.warning("Batch %s file indexing failed (%s)", batch_id, type(exc).__name__)
            with transaction() as conn:
                conn.execute(
                    "UPDATE batch_import_files SET status=?,document_id=?,error=?,updated_at=? WHERE id=?",
                    (status, document_id, error, now(), item["id"]),
                )
            _batch_progress(batch_id)
            if cancelled:
                raise asyncio.CancelledError

    # Shared by all batch jobs in one worker, not unbounded per HTTP request.
    jobs = [asyncio.create_task(process_one(item)) for item in pending]
    try:
        await asyncio.gather(*jobs)
    except BaseException:
        # gather can surface a waiting sibling's cancellation before an active
        # sibling has finished its shielded write. Drain every child first.
        for job in jobs:
            if not job.done() and not job.cancelling():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        raise
    result = _batch_progress(batch_id, finalize=True)
    cleanup_batch_files(batch_id)
    return result


async def worker_startup(ctx: dict[str, Any]) -> None:
    init_db(seed=False)
    ctx["indexing_semaphore"] = asyncio.Semaphore(max(1, config.indexing_concurrency))
    # The worker owns a live Redis connection, so it can discharge batches whose
    # enqueue outcome was never confirmed (crash between commit and enqueue).
    await reconcile_batch_dispatches()


def _load_pending_dispatches() -> list[dict[str, Any]]:
    with transaction() as conn:
        return [dict(row) for row in conn.execute(
            """SELECT d.batch_id, d.case_id, d.attempts
               FROM batch_dispatch d JOIN batch_imports b ON b.id = d.batch_id
               WHERE d.state = 'pending' AND b.status = 'queued'
               ORDER BY d.batch_id"""
        ).fetchall()]


def _dispatch_files(batch_id: int) -> list[dict[str, Any]]:
    with transaction() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT file_key, filename, stored_path, mime_type FROM batch_import_files WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()]


def _mark_dispatched(batch_id: int, *, success: bool) -> None:
    with transaction() as conn:
        state = "discharged" if success else "pending"
        conn.execute(
            "UPDATE batch_dispatch SET state=?, attempts=attempts+1, updated_at=? WHERE batch_id=?",
            (state, now(), batch_id),
        )


async def reconcile_batch_dispatches() -> int:
    """Re-enqueue registered batches with unconfirmed enqueue outcomes.

    Deterministic job IDs make this at-least-once: arq deduplicates a still
    queued job, and process_batch_import resumes only unfinished files.
    """
    dispatched = 0
    for entry in _load_pending_dispatches():
        batch_id = entry["batch_id"]
        files = _dispatch_files(batch_id)
        if not files:
            # A queued batch without staged files cannot be processed; keep it
            # pending so the state stays visible instead of vanishing silently.
            logger.warning("Batch %s has no staged files; dispatch stays pending", batch_id)
            continue
        try:
            await enqueue_batch_import(batch_id, entry["case_id"], files)
        except Exception as exc:
            logger.warning("Batch %s re-dispatch failed (%s)", batch_id, type(exc).__name__)
            _mark_dispatched(batch_id, success=False)
            continue
        _mark_dispatched(batch_id, success=True)
        dispatched += 1
    return dispatched


class WorkerSettings:
    functions = [process_batch_import]
    on_startup = worker_startup
    redis_settings = RedisSettings.from_dsn(config.redis.url)
    job_timeout = config.redis.job_timeout
    max_jobs = config.redis.max_jobs
    queue_name = config.redis.queue_name


async def enqueue_batch_import(batch_id: int, case_id: int, files: list[dict]) -> str:
    pool = await get_redis_pool()
    job_id = f"batch-import-{batch_id}"
    try:
        job = await pool.enqueue_job(
            "process_batch_import", batch_id, case_id, files,
            _queue_name=config.redis.queue_name, _job_id=job_id,
        )
        # arq returns None if this deterministic job ID already exists.
        return job.job_id if job is not None else job_id
    finally:
        try:
            await pool.aclose()
        except Exception:
            # A pool shutdown failure cannot revoke an already accepted job.
            logger.exception("Could not close batch enqueue Redis pool")
