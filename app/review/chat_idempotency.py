"""Idempotent chat submission.

A browser that loses its connection mid-answer cannot know whether the turn was
committed. Retrying blindly doubles the question; not retrying loses the answer.
This module records one row per client-generated ``request_id`` in the same
transaction that writes the messages, so a retry can either replay the committed
result or start over when nothing was written.

The row is keyed by ``(case_id, request_id)``: a request id presented for another
case must never replay that case's answer. States:

``in_progress``  a worker is running; a concurrent duplicate is refused
``committed``    messages are written; ``result_json`` replays the turn
``failed``       nothing was written; a retry may run again
``cancelled``    the client disconnected before the commit boundary
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.storage.db import now, transaction

IN_PROGRESS = "in_progress"
COMMITTED = "committed"
FAILED = "failed"
CANCELLED = "cancelled"

# A crashed worker leaves ``in_progress`` behind. After this long the row is
# treated as failed so a retry can proceed instead of blocking forever.
STALE_IN_PROGRESS = timedelta(minutes=15)

# Committed rows duplicate the message content so a retry can be replayed. They
# outlive their usefulness quickly, so they are swept at most once an hour.
RETENTION = timedelta(days=7)
_PURGE_INTERVAL_SECONDS = 3600.0
_last_purge = 0.0

REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"


class ChatCancelled(Exception):
    """The client disconnected before the turn reached its commit boundary."""


class RequestInProgress(Exception):
    """The same request id is already being processed by a live worker."""


@dataclass(frozen=True)
class BeginResult:
    state: str
    result: dict[str, Any] | None
    conversation_id: int | None
    message_id: int | None


def _row(conn: sqlite3.Connection, case_id: int, request_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT case_id, request_id, conversation_id, status, result_json, message_id, updated_at "
        "FROM chat_requests WHERE case_id=? AND request_id=?",
        (case_id, request_id),
    ).fetchone()


def begin(case_id: int, request_id: str) -> BeginResult:
    """Claim a request id, or report what already happened to it.

    Raises ``RequestInProgress`` when a live duplicate is running. A stale
    ``in_progress`` row (crashed worker) is converted to ``failed`` and the claim
    proceeds.
    """
    ts = now()
    with transaction() as conn:
        _maybe_purge(conn)
        row = _row(conn, case_id, request_id)
        if row is None:
            conn.execute(
                """INSERT INTO chat_requests(case_id, request_id, conversation_id, status,
                                             result_json, message_id, created_at, updated_at)
                   VALUES (?, ?, NULL, ?, NULL, NULL, ?, ?)""",
                (case_id, request_id, IN_PROGRESS, ts, ts),
            )
            return BeginResult(IN_PROGRESS, None, None, None)
        status = str(row["status"])
        if status == COMMITTED:
            payload = json.loads(row["result_json"] or "{}")
            return BeginResult(COMMITTED, payload, int(row["conversation_id"] or 0) or None, row["message_id"])
        if status == IN_PROGRESS:
            if _is_stale(str(row["updated_at"])):
                conn.execute(
                    "UPDATE chat_requests SET status=?, updated_at=? WHERE case_id=? AND request_id=?",
                    (FAILED, ts, case_id, request_id),
                )
            else:
                raise RequestInProgress(request_id)
        conn.execute(
            """UPDATE chat_requests SET conversation_id=NULL, result_json=NULL, message_id=NULL,
                                        status=?, updated_at=?
               WHERE case_id=? AND request_id=?""",
            (IN_PROGRESS, ts, case_id, request_id),
        )
        return BeginResult(IN_PROGRESS, None, None, None)


def _maybe_purge(conn: sqlite3.Connection) -> None:
    global _last_purge
    current = time.monotonic()
    if current - _last_purge < _PURGE_INTERVAL_SECONDS:
        return
    _last_purge = current
    moment = datetime.now().astimezone()
    conn.execute("DELETE FROM chat_requests WHERE updated_at < ?",
                 ((moment - RETENTION).isoformat(timespec="seconds"),))


def _is_stale(updated_at: str) -> bool:
    try:
        moment = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    if moment.tzinfo is None:
        return True
    return datetime.now(moment.tzinfo) - moment > STALE_IN_PROGRESS


def commit(conn: sqlite3.Connection, case_id: int, request_id: str, *, conversation_id: int,
           message_id: int | None, result: dict[str, Any]) -> None:
    """Mark the request committed *inside* the caller's transaction.

    Sharing the transaction is the entire point: either the messages and the
    replay payload become visible together, or neither does.
    """
    conn.execute(
        """UPDATE chat_requests SET status=?, conversation_id=?, message_id=?, result_json=?, updated_at=?
           WHERE case_id=? AND request_id=?""",
        (COMMITTED, conversation_id, message_id, json.dumps(result, ensure_ascii=False), now(),
         case_id, request_id),
    )


def finish(case_id: int, request_id: str, status: str) -> None:
    """Mark a request failed/cancelled after leaving the commit boundary."""
    if status not in {FAILED, CANCELLED}:
        raise ValueError("only failed or cancelled may be recorded here")
    with transaction() as conn:
        conn.execute(
            "UPDATE chat_requests SET status=?, updated_at=? WHERE case_id=? AND request_id=? AND status=?",
            (status, now(), case_id, request_id, IN_PROGRESS),
        )


def lookup(case_id: int, request_id: str) -> BeginResult | None:
    """Read the stored state without claiming anything."""
    with transaction() as conn:
        row = _row(conn, case_id, request_id)
    if row is None:
        return None
    payload = json.loads(row["result_json"] or "{}") if row["status"] == COMMITTED else None
    return BeginResult(
        str(row["status"]),
        payload,
        int(row["conversation_id"] or 0) or None,
        row["message_id"],
    )
