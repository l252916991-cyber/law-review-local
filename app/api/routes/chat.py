"""Conversation, retrieval chat (JSON and SSE), read-only tool and analysis endpoints."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import queue
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.routes.common import model_operation_error, require_case
from app.api.schemas import ChatResult, ConversationSummary, Message
from app.cases.gap_analysis import auto_analyze_case
from app.core.security import actor_name
from app.llm.llm_usage import token_counts
from app.review.agent_tools import execute_readonly_tool
from app.review.chat import chat
from app.review.chat_idempotency import CANCELLED as CHAT_REQUEST_CANCELLED
from app.review.chat_idempotency import COMMITTED as CHAT_REQUEST_COMMITTED
from app.review.chat_idempotency import FAILED as CHAT_REQUEST_FAILED
from app.review.chat_idempotency import REQUEST_ID_PATTERN, ChatCancelled, RequestInProgress
from app.review.chat_idempotency import begin as begin_chat_request
from app.review.chat_idempotency import finish as finish_chat_request
from app.review.context import ArchivedConversationError, ConversationNotFoundError
from app.storage.db import (
    CONVERSATION_ARCHIVE_RETENTION_SECONDS,
    connect,
    now,
    purge_expired_conversations,
    rowdict,
    transaction,
)

router = APIRouter()
access_logger = logging.getLogger("law_review.access")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=3000)
    user_name: str = "本机律师"
    conversation_id: int | None = None
    use_llm: bool = True
    use_remote_embeddings: bool = True
    # Optional idempotency key. The desktop client reuses one key for its
    # automatic retry so a disconnect cannot produce a duplicate turn.
    request_id: str | None = Field(default=None, pattern=REQUEST_ID_PATTERN)


class ReadonlyToolCall(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/cases/{case_id}/agent-tools/call")
def call_readonly_agent_tool(case_id: int, payload: ReadonlyToolCall):
    """Call one bounded view-only tool; returned case content is untrusted data."""
    require_case(case_id)
    try:
        return execute_readonly_tool(case_id, payload.tool, payload.params)
    except KeyError as exc:
        raise HTTPException(404, "只读工具不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise model_operation_error(exc, "只读检索工具失败，请查看服务端诊断事件") from exc


@router.post("/api/cases/{case_id}/analyze")
def analyze(case_id: int):
    require_case(case_id)
    return auto_analyze_case(case_id)


@router.get("/api/cases/{case_id}/conversations", response_model=list[ConversationSummary])
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


@router.post("/api/conversations/{conversation_id}/archive")
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


@router.delete("/api/conversations/{conversation_id}")
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


@router.get("/api/conversations/{conversation_id}/messages", response_model=list[Message])
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


def _mark_chat_request(case_id: int, request_id: str, status: str) -> None:
    """Best-effort bookkeeping; never mask the real error with a marker failure."""
    try:
        finish_chat_request(case_id, request_id, status)
    except Exception:  # pragma: no cover - the original exception matters more
        access_logger.exception("Could not mark the chat request as %s", status)


def _replay_chat_result(began: Any) -> dict[str, Any]:
    stored = dict(began.result or {})
    stored["request_id_replayed"] = True
    return stored


@router.post("/api/cases/{case_id}/chat", response_model=ChatResult)
def case_chat(case_id: int, payload: ChatRequest):
    require_case(case_id)
    request_id = payload.request_id
    try:
        if request_id is not None:
            began = begin_chat_request(case_id, request_id)
            if began.state == CHAT_REQUEST_COMMITTED:
                # A retry after a lost response returns the stored turn: the
                # client gets its answer and the database keeps one message pair.
                return _replay_chat_result(began)
        try:
            return chat(
                case_id,
                payload.question,
                actor_name(payload.user_name),
                payload.conversation_id,
                payload.use_llm,
                payload.use_remote_embeddings,
                request_key=(case_id, request_id) if request_id is not None else None,
            )
        except Exception:
            if request_id is not None:
                _mark_chat_request(case_id, request_id, CHAT_REQUEST_FAILED)
            raise
    except RequestInProgress as exc:
        raise HTTPException(409, "同一请求正在处理，请勿重复提交") from exc
    except ArchivedConversationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ConversationNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise model_operation_error(exc, "问答失败，请查看服务端诊断事件") from exc


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_usage_payload() -> dict[str, Any] | None:
    """Usage actually reported for this turn; unknown stays ``None``."""
    from app.review.generation import last_llm_usage

    observed = last_llm_usage() or {}
    input_tokens, output_tokens = token_counts(observed.get("usage"))
    if input_tokens is None and output_tokens is None:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": observed.get("model") or None,
    }


def _stream_done(result: dict[str, Any], *, ttft_ms: int | None, request_id: str | None,
                 message_id: int | None) -> dict[str, Any]:
    """Terminal success frame. Chat turns have no ``run_id``; never invent one."""
    return {
        "status": "ok",
        "conversation_id": result.get("conversation_id"),
        "message_id": message_id,
        "answer_status": result.get("answer_status"),
        "citation_check": result.get("citation_check"),
        "llm_used": bool(result.get("llm_used")),
        "ttft_ms": ttft_ms,
        "usage": _stream_usage_payload(),
        "request_id": request_id,
    }


def _stream_failure(code: str, message: str, *, ttft_ms: int | None,
                    request_id: str | None) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("retract", {"code": code, "message": message}),
        ("done", {
            "status": "failed",
            "code": code,
            "retracted": True,
            "conversation_id": None,
            "message_id": None,
            "ttft_ms": ttft_ms,
            "usage": None,
            "request_id": request_id,
        }),
    ]


def _replay_frames(result: dict[str, Any], *, request_id: str, message_id: int | None) -> Any:
    """Re-emit a committed turn. Per-turn TTFT/usage are not re-invented."""
    citations = list(result.get("citations") or [])
    yield _sse_frame("status", {"phase": "replayed", "request_id": request_id})
    yield _sse_frame("citations", {"citations": citations, "count": len(citations)})
    yield _sse_frame("final", result)
    yield _sse_frame("done", {
        "status": "ok",
        "replayed": True,
        "conversation_id": result.get("conversation_id"),
        "message_id": message_id,
        "answer_status": result.get("answer_status"),
        "citation_check": result.get("citation_check"),
        "llm_used": bool(result.get("llm_used")),
        "ttft_ms": None,
        "usage": None,
        "request_id": request_id,
    })


@router.post("/api/cases/{case_id}/chat/stream")
async def case_chat_stream(case_id: int, payload: ChatRequest, request: Request):
    """Token stream for one turn; the answer contract still validates in full.

    Event contract (A01.1):

    ``status``    ``phase`` = retrieval / generating / validating / replayed
    ``token``     provisional draft text (``provisional: true``); never a fact
    ``citations`` structured citations, emitted only after the contract passed
    ``final``     the complete answer payload; kept for older desktop builds
    ``retract``   failure terminal: discard the draft, nothing was committed
    ``done``      always last; ``status`` ok|failed, ids, ``ttft_ms``, ``usage``

    Tokens are provisional: they reach the browser before ``validate_review_answer``
    has run. Streaming must never publish an answer that the non-streaming path
    would have rejected. A disconnect stops further business writes (the worker
    checks a cancel flag at the commit boundary); if the commit already happened,
    a retry with the same ``request_id`` replays the stored turn instead of
    appending a second one.
    """
    require_case(case_id)
    request_id = payload.request_id
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    if request_id is not None:
        try:
            began = begin_chat_request(case_id, request_id)
        except RequestInProgress as exc:
            raise HTTPException(409, "同一请求正在处理，请勿重复提交") from exc
        if began.state == CHAT_REQUEST_COMMITTED and began.result is not None:
            return StreamingResponse(
                _replay_frames(began.result, request_id=request_id,
                               message_id=began.result.get("message_id") or began.message_id),
                media_type="text/event-stream", headers=headers,
            )

    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    cancelled = threading.Event()
    timing: dict[str, int | None] = {"ttft_ms": None}

    def emit(kind: str, data: Any) -> None:
        events.put((kind, data))

    def worker() -> None:
        started = time.perf_counter()

        def on_token(text: str) -> None:
            if timing["ttft_ms"] is None:
                timing["ttft_ms"] = int((time.perf_counter() - started) * 1000)
            emit("token", {"text": text, "provisional": True})

        def on_phase(name: str) -> None:
            emit("status", {"phase": name})

        def fail(code: str, message: str) -> None:
            if request_id is not None:
                _mark_chat_request(case_id, request_id, CHAT_REQUEST_FAILED)
            for kind, data in _stream_failure(code, message, ttft_ms=timing["ttft_ms"], request_id=request_id):
                emit(kind, data)

        try:
            result = chat(
                case_id,
                payload.question,
                actor_name(payload.user_name),
                payload.conversation_id,
                payload.use_llm,
                payload.use_remote_embeddings,
                on_token=on_token,
                on_phase=on_phase,
                should_cancel=cancelled.is_set,
                request_key=(case_id, request_id) if request_id is not None else None,
            )
        except ChatCancelled:
            # The disconnect won the race: no messages were written, so the id
            # is released for a clean retry. Model usage already recorded stays.
            if request_id is not None:
                _mark_chat_request(case_id, request_id, CHAT_REQUEST_CANCELLED)
            emit("done", {"status": "cancelled", "request_id": request_id})
            return
        except ArchivedConversationError as exc:
            fail("archived_conversation", str(exc))
            return
        except ConversationNotFoundError as exc:
            fail("conversation_not_found", str(exc))
            return
        except ValueError as exc:
            fail("invalid_request", str(exc))
            return
        except RuntimeError:
            access_logger.warning("Streaming chat failed")
            fail("model_error", "模型调用失败，已撤回未校验草稿")
            return
        except Exception:
            access_logger.exception("Streaming chat crashed")
            fail("internal_error", "问答失败，请查看服务端诊断事件")
            return
        citations = list(result.get("citations") or [])
        emit("citations", {"citations": citations, "count": len(citations)})
        emit("final", result)
        emit("done", _stream_done(
            result, ttft_ms=timing["ttft_ms"], request_id=request_id,
            message_id=result.get("message_id"),
        ))

    # A worker thread starts with an empty context. Copy the request context so
    # the scoped database path, the authenticated principal and the request id
    # stay visible to the same business code that runs in the blocking path.
    context = contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(worker,), name="lexvault-chat-stream", daemon=True)
    thread.start()

    async def generator():
        # One status frame before the first token: the client can show progress
        # instead of an empty bubble while retrieval runs.
        try:
            yield _sse_frame("status", {"phase": "retrieval"})
            while True:
                if await request.is_disconnected():
                    return
                try:
                    kind, data = await asyncio.to_thread(events.get, True, 15)
                except queue.Empty:
                    # Comment frames keep intermediaries from closing an idle stream.
                    yield ": keepalive\n\n"
                    continue
                yield _sse_frame(kind, data if isinstance(data, dict) else {"value": data})
                if kind == "done":
                    return
        finally:
            # Runs on normal completion too; the worker is already past its
            # commit boundary there, so the flag is only consulted on the way out.
            cancelled.set()

    return StreamingResponse(generator(), media_type="text/event-stream", headers=headers)

