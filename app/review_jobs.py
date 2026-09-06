"""Bounded local background review jobs with durable status and polling.

Single app process only. A restart marks unfinished jobs interrupted; LangGraph
run checkpoints remain resumable through the existing run API.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .db import connect, db_scope, get_db_path, now, transaction
from .security import actor_name, current_principal, require_case_access

router = APIRouter(tags=["review-jobs"])
logger = logging.getLogger(__name__)
_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=2, thread_name_prefix="review-job")
_admission = threading.BoundedSemaphore(4)
_lifecycle_lock = threading.Lock()


def start_review_executor() -> None:
    global _executor, _admission
    with _lifecycle_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="review-job")
            _admission = threading.BoundedSemaphore(4)


def shutdown_review_executor() -> None:
    """Cancel queued jobs and drain running writes before app shutdown."""
    global _executor
    with _lifecycle_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


class ReviewJobRequest(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    user_name: str = Field(default="本机律师", max_length=120)
    mode: Literal["multi_agent", "langgraph"] = "multi_agent"
    use_llm: bool = True
    use_remote_embeddings: bool = True


def _execute(job_id: str, case_id: int, body: ReviewJobRequest) -> None:
    run_id = None
    try:
        with transaction() as conn:
            claimed = conn.execute("UPDATE review_jobs SET status='running' WHERE id=? AND status='queued'", (job_id,))
            if claimed.rowcount != 1:
                return
        if body.mode == "langgraph":
            from .langgraph_agents import create_langgraph_coordinator as factory
        else:
            from .agents import create_coordinator as factory
        coordinator = factory(case_id, body.use_remote_embeddings)
        original_start = coordinator._start_run

        def track_start(*args, **kwargs):
            nonlocal run_id
            started = original_start(*args, **kwargs)
            run_id = started[0] if isinstance(started, tuple) else started
            with transaction() as conn:
                conn.execute("UPDATE review_jobs SET run_id=? WHERE id=?", (run_id, job_id))
            return started

        coordinator._start_run = track_start
        try:
            result = coordinator.process_query(body.question, body.user_name, body.use_llm)
        finally:
            coordinator._start_run = original_start
        result = {**result, "mode": body.mode, "user_name": body.user_name}
        with transaction() as conn:
            job = conn.execute("SELECT status FROM review_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] != "running":
                return
            conversation = conn.execute("INSERT INTO conversations(case_id,user_name,title,created_at) VALUES (?,?,?,?)",
                                        (case_id, body.user_name, body.question[:60], now())).lastrowid
            for role, content in (("user", body.question), ("assistant", result["answer"])):
                conn.execute("INSERT INTO messages(conversation_id,role,content,citations_json,route,created_at) VALUES (?,?,?,?,?,?)",
                             (conversation,role,content,json.dumps(result.get("citations", []) if role == "assistant" else [],ensure_ascii=False),result["route"],now()))
            result["conversation_id"] = conversation
            conn.execute("UPDATE review_jobs SET status='completed',result_json=?,finished_at=? WHERE id=?",
                         (json.dumps(result,ensure_ascii=False),now(),job_id))
            conn.execute("INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'后台 Agent 阅卷',?,?)",
                         (case_id, f"{body.user_name}：任务 {job_id}", now()))
    except Exception as exc:
        diagnostic = {"type": type(exc).__name__, "message": "后台阅卷执行失败，请查看节点轨迹或恢复可用的 LangGraph 运行", "run_id": getattr(exc,"run_id",None) or run_id, "resumable": bool(getattr(exc,"resumable",False))}
        logger.warning("Review job %s failed (%s)", job_id, type(exc).__name__)
        with transaction() as conn:
            conn.execute("UPDATE review_jobs SET status='failed',error_json=?,run_id=COALESCE(run_id,?),finished_at=? WHERE id=? AND status='running'",
                         (json.dumps(diagnostic,ensure_ascii=False),diagnostic["run_id"],now(),job_id))


@router.post("/api/cases/{case_id}/agent-jobs", status_code=202)
def submit_review(case_id: int, body: ReviewJobRequest):
    require_case_access(case_id)
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone():
            raise HTTPException(404, "案件不存在")
    with _lifecycle_lock:
        executor, admission = _executor, _admission
        if executor is None:
            raise HTTPException(503, "后台阅卷服务正在关闭")
        if not admission.acquire(blocking=False):
            raise HTTPException(429, "后台阅卷队列已满，请稍后重试")
    job_id = uuid.uuid4().hex
    path = get_db_path()
    try:
        body = body.model_copy(update={"user_name": actor_name(body.user_name)})
        with transaction() as conn:
            conn.execute("INSERT INTO review_jobs(id,case_id,principal,status,request_json,created_at) VALUES (?,?,?,'queued',?,?)",
                         (job_id,case_id,current_principal().name,body.model_dump_json(),now()))
        # An unbound ContextVar reads the environment dynamically. Freeze the
        # resolved DB path as well as the authenticated principal before submit.
        with db_scope(path):
            context = copy_context()
        future = executor.submit(context.run, _execute, job_id, case_id, body)
    except Exception as exc:
        admission.release()
        try:
            with transaction() as conn:
                conn.execute("UPDATE review_jobs SET status='failed',error_json=?,finished_at=? WHERE id=?",
                             (json.dumps({"type": type(exc).__name__, "message": "后台任务提交失败"}), now(), job_id))
        except Exception:
            logger.error("Could not persist submission failure for job %s", job_id)
        raise HTTPException(503, {"message": "后台阅卷任务提交失败，请稍后重试", "job_id": job_id}) from exc

    def finished(completed):
        try:
            if completed.cancelled():
                with db_scope(path), transaction() as conn:
                    conn.execute("UPDATE review_jobs SET status='interrupted',error_json=?,finished_at=? WHERE id=? AND status='queued'",
                                 (json.dumps({"type": "Cancelled", "message": "服务关闭，排队任务未执行"}), now(), job_id))
        except Exception:
            logger.error("Could not persist cancellation for job %s", job_id)
        finally:
            admission.release()

    # Admission belongs to the Future, including cancellation before execution.
    future.add_done_callback(finished)
    return {"job_id": job_id, "status": "queued", "poll_url": f"/api/agent-jobs/{job_id}"}


@router.get("/api/agent-jobs/{job_id}")
def review_status(job_id: str):
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "后台任务不存在")
    require_case_access(row["case_id"])
    trace = {}
    if row["run_id"]:
        from .agents import get_run_trace
        trace = get_run_trace(row["run_id"])
    error = json.loads(row["error_json"])
    if row["status"] == "interrupted" and not error:
        error = {"type": "Interrupted", "message": "服务重启中断了任务；存在 LangGraph 检查点时可继续执行"}
    return {"job_id": job_id, "case_id": row["case_id"], "status": row["status"], "run_id": row["run_id"],
            "steps": trace.get("steps", []), "result": json.loads(row["result_json"]),
            "error": error, "resumable": bool(trace.get("resumable", False)),
            "runtime": trace.get("runtime", "langgraph" if json.loads(row["request_json"]).get("mode") == "langgraph" else "native"),
            "created_at": row["created_at"], "finished_at": row["finished_at"]}
