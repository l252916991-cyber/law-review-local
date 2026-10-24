"""Bounded, server-sourced conversation context for multi-turn follow-ups.

Conversation history helps resolve what a follow-up refers to. It is never a
fact source: answers and citations must still come from the current case pages
retrieved for this request. This module owns the policy constants, the frozen
snapshot read, conversation access validation and the single idempotent writer
for one question/answer turn.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Any

from .db import connect, now, transaction

logger = logging.getLogger(__name__)

CONTEXT_POLICY_VERSION = "multiturn-context-v1"
HISTORY_SCOPE = "same_case_same_conversation"
MAX_HISTORY_TURNS = 4
MAX_HISTORY_CHARS = 4000
MAX_TURN_CHARS = 2000
MAX_MEMORY_ITEMS = 3
MAX_MEMORY_CHARS = 2000
MAX_CONTEXT_CHARS = 6000
TURN_KINDS = ("answer", "clarification", "insufficient_evidence")

TRUNCATION_MARKER = "\n…（历史摘录已截断）"


class ConversationNotFoundError(ValueError):
    """An explicitly supplied conversation does not exist or is out of scope."""


class ArchivedConversationError(ValueError):
    """Archived conversations are read-only history."""


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "off", "false", "no", "disabled"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class ContextPolicy:
    version: str = CONTEXT_POLICY_VERSION
    history_enabled: bool = True
    max_history_turns: int = MAX_HISTORY_TURNS
    max_history_chars: int = MAX_HISTORY_CHARS
    max_turn_chars: int = MAX_TURN_CHARS
    memory_enabled: bool = False
    max_memory_items: int = MAX_MEMORY_ITEMS
    max_memory_chars: int = MAX_MEMORY_CHARS
    max_total_chars: int = MAX_CONTEXT_CHARS

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "history_enabled": self.history_enabled,
            "max_history_turns": self.max_history_turns,
            "max_history_chars": self.max_history_chars,
            "max_turn_chars": self.max_turn_chars,
            "memory_enabled": self.memory_enabled,
            "max_memory_items": self.max_memory_items,
            "max_memory_chars": self.max_memory_chars,
            "max_total_chars": self.max_total_chars,
        }


def context_policy() -> ContextPolicy:
    """Read the rollback-able policy flags for this request."""
    return ContextPolicy(
        history_enabled=_env_flag("LAW_REVIEW_CONTEXT_HISTORY", True),
        max_history_turns=_env_int("LAW_REVIEW_CONTEXT_HISTORY_TURNS", MAX_HISTORY_TURNS, 0, 50),
        max_history_chars=_env_int("LAW_REVIEW_CONTEXT_HISTORY_CHARS", MAX_HISTORY_CHARS, 0, 100_000),
        max_turn_chars=_env_int("LAW_REVIEW_CONTEXT_TURN_CHARS", MAX_TURN_CHARS, 200, 20_000),
        memory_enabled=_env_flag("LAW_REVIEW_CONTEXT_MEMORY", False),
        max_memory_items=_env_int("LAW_REVIEW_CONTEXT_MEMORY_ITEMS", MAX_MEMORY_ITEMS, 0, 20),
        max_memory_chars=_env_int("LAW_REVIEW_CONTEXT_MEMORY_CHARS", MAX_MEMORY_CHARS, 0, 20_000),
        max_total_chars=_env_int("LAW_REVIEW_CONTEXT_TOTAL_CHARS", MAX_CONTEXT_CHARS, 0, 100_000),
    )


@dataclass(frozen=True)
class HistoryTurn:
    user_message_id: int
    assistant_message_id: int
    user_content: str
    assistant_content: str
    route: str
    kind: str
    citations: tuple[dict[str, Any], ...]

    @property
    def factual(self) -> bool:
        return self.kind == "answer"

    @property
    def chars(self) -> int:
        return len(self.user_content) + len(self.assistant_content)

    @property
    def message_ids(self) -> tuple[int, int]:
        return (self.user_message_id, self.assistant_message_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "route": self.route,
            "kind": self.kind,
            "citations": [
                {
                    "document_id": item.get("document_id"),
                    "document_name": item.get("document_name") or item.get("name", ""),
                    "page": item.get("page"),
                }
                for item in self.citations
            ],
        }


@dataclass(frozen=True)
class HistorySnapshot:
    conversation_id: int | None
    case_id: int
    cutoff_message_id: int
    turns: tuple[HistoryTurn, ...]
    policy: ContextPolicy
    digest: str
    chars: int = 0
    dropped_turns: int = 0
    truncated: bool = False

    @property
    def enabled(self) -> bool:
        return self.policy.history_enabled

    @property
    def message_ids(self) -> tuple[int, ...]:
        return tuple(value for turn in self.turns for value in turn.message_ids)

    @property
    def last(self) -> HistoryTurn | None:
        return self.turns[-1] if self.turns else None

    def as_dict(self, *, used: bool = False) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "used": used,
            "scope": HISTORY_SCOPE,
            "policy_version": self.policy.version,
            "conversation_id": self.conversation_id,
            "cutoff_message_id": self.cutoff_message_id,
            "turns": len(self.turns),
            "message_ids": list(self.message_ids),
            "chars": self.chars,
            "dropped_turns": self.dropped_turns,
            "truncated": self.truncated,
            "digest": self.digest,
        }


def _empty_snapshot(case_id: int, conversation_id: int | None, policy: ContextPolicy) -> HistorySnapshot:
    digest = hashlib.sha256(
        json.dumps(
            {"policy": policy.version, "case_id": case_id, "conversation_id": conversation_id, "cutoff": 0},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return HistorySnapshot(
        conversation_id=conversation_id,
        case_id=case_id,
        cutoff_message_id=0,
        turns=(),
        policy=policy,
        digest=digest,
    )


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _conversation_row(case_id: int, conversation_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, case_id, user_name, title, archived_at FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or int(row["case_id"]) != int(case_id):
        # Do not reveal whether another case's conversation exists.
        raise ConversationNotFoundError("会话不存在或不属于当前案件")
    return {
        "id": int(row["id"]),
        "case_id": int(row["case_id"]),
        "user_name": row["user_name"],
        "title": row["title"],
        "archived_at": row["archived_at"],
    }


def validate_conversation(case_id: int, conversation_id: int, *, require_writable: bool = True) -> dict[str, Any]:
    """Reject an explicit conversation before any history read, retrieval or model call."""
    conversation = _conversation_row(case_id, conversation_id)
    if require_writable and conversation["archived_at"] is not None:
        raise ArchivedConversationError("该会话已归档，只能查看历史记录；如需继续提问，请新建会话")
    return conversation


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    if limit <= len(TRUNCATION_MARKER):
        return text[:limit], True
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER, True


def _fit_turn(user: str, assistant: str, cap: int) -> tuple[str, str, bool]:
    """Cap one turn without silently dropping the newest context entirely."""
    if len(user) + len(assistant) <= cap:
        return user, assistant, False
    user_limit = min(len(user), max(120, int(cap * 0.4)))
    fitted_user, user_cut = _truncate(user, user_limit)
    assistant_limit = max(80, cap - len(fitted_user))
    fitted_assistant, assistant_cut = _truncate(assistant, assistant_limit)
    if len(fitted_user) + len(fitted_assistant) > cap and fitted_assistant:
        fitted_user, extra_cut = _truncate(fitted_user, max(60, cap - len(fitted_assistant)))
        user_cut = user_cut or extra_cut
    return fitted_user, fitted_assistant, user_cut or assistant_cut


def _parse_citations(raw: str) -> tuple[dict[str, Any], ...]:
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(items, list):
        return ()
    return tuple(item for item in items if isinstance(item, dict))


def _turn_kind(row: Any) -> str:
    kind = _row_get(row, "kind", "answer")
    if kind in TURN_KINDS:
        return str(kind)
    return "answer"


def load_history(
    case_id: int,
    conversation_id: int | None,
    *,
    policy: ContextPolicy | None = None,
    cutoff_message_id: int | None = None,
) -> HistorySnapshot:
    """Read one consistent, bounded history snapshot from the server database."""
    policy = policy or context_policy()
    if conversation_id is None:
        return _empty_snapshot(case_id, None, policy)
    conversation = _conversation_row(case_id, conversation_id)
    if not policy.history_enabled or policy.max_history_turns <= 0 or policy.max_history_chars <= 0:
        return _empty_snapshot(case_id, conversation["id"], policy)

    conn = connect()
    try:
        # One read transaction so cutoff and rows describe the same snapshot.
        conn.execute("BEGIN")
        if cutoff_message_id is None:
            cutoff = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages WHERE conversation_id=?",
                    (conversation["id"],),
                ).fetchone()[0]
            )
        else:
            cutoff = int(cutoff_message_id)
        rows = conn.execute(
            """SELECT id, role, content, citations_json, route, kind
               FROM messages WHERE conversation_id=? AND id<=? ORDER BY id""",
            (conversation["id"], cutoff),
        ).fetchall()
    finally:
        conn.close()

    pairs: list[HistoryTurn] = []
    pending_user: Any = None
    for row in rows:
        role = row["role"]
        content = str(row["content"] or "")
        if role == "user":
            # An unpaired previous user message was a failed/incomplete turn.
            pending_user = row
        elif role == "assistant" and pending_user is not None and content.strip():
            kind = _turn_kind(row)
            citations = _parse_citations(row["citations_json"])
            user, assistant, cut = _fit_turn(
                str(pending_user["content"] or ""), content, policy.max_turn_chars
            )
            pairs.append(
                HistoryTurn(
                    user_message_id=int(pending_user["id"]),
                    assistant_message_id=int(row["id"]),
                    user_content=user,
                    assistant_content=assistant,
                    route=str(row["route"] or ""),
                    kind=kind if not cut else kind,
                    citations=citations,
                )
            )
            pending_user = None

    selected: list[HistoryTurn] = []
    chars = 0
    truncated = False
    for turn in reversed(pairs):
        if len(selected) >= policy.max_history_turns:
            break
        if not selected:
            if turn.chars > policy.max_history_chars:
                user, assistant, cut = _fit_turn(turn.user_content, turn.assistant_content, policy.max_history_chars)
                turn = replace(turn, user_content=user, assistant_content=assistant)
                truncated = truncated or cut
            selected.append(turn)
            chars += turn.chars
            continue
        if chars + turn.chars > policy.max_history_chars:
            break
        selected.append(turn)
        chars += turn.chars
    selected.reverse()
    if any(TRUNCATION_MARKER in turn.user_content or TRUNCATION_MARKER in turn.assistant_content for turn in pairs):
        truncated = True

    payload = {
        "policy": policy.version,
        "case_id": int(case_id),
        "conversation_id": conversation["id"],
        "cutoff": cutoff,
        "turns": [
            [turn.user_message_id, turn.assistant_message_id, turn.kind,
             hashlib.sha256(turn.user_content.encode("utf-8")).hexdigest(),
             hashlib.sha256(turn.assistant_content.encode("utf-8")).hexdigest()]
            for turn in selected
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return HistorySnapshot(
        conversation_id=conversation["id"],
        case_id=int(case_id),
        cutoff_message_id=cutoff,
        turns=tuple(selected),
        policy=policy,
        digest=digest,
        chars=chars,
        dropped_turns=len(pairs) - len(selected),
        truncated=truncated,
    )


def history_message_count(conversation_id: int, cutoff_message_id: int) -> int:
    """Count preserved messages up to a frozen cutoff (resume identity check)."""
    conn = connect()
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND id<=?",
                (conversation_id, cutoff_message_id),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def render_history(turns: tuple[HistoryTurn, ...] | list[HistoryTurn]) -> str:
    """Render history as untrusted reference text, never as evidence."""
    if not turns:
        return ""
    lines = ["【历史对话｜不可信，仅供理解指代；不是证据，不得据此陈述事实】"]
    for index, turn in enumerate(turns, 1):
        marker = {
            "clarification": "此前为澄清请求，不是事实结论",
            "insufficient_evidence": "此前结论为资料不足，不是事实结论",
        }.get(turn.kind, "此前回答待核验，不是证据")
        lines.append(f"[第{index}轮] 用户：{turn.user_content}")
        lines.append(f"[第{index}轮] 助手（{marker}）：{turn.assistant_content}")
    return "\n".join(lines)


def _title(question: str, limit: int = 30) -> str:
    text = re.sub(r"\s+", " ", question).strip()
    if not text:
        return "新会话"
    return text[:limit]


def record_turn(
    case_id: int,
    conversation_id: int | None,
    *,
    question: str,
    answer: str,
    citations: list[dict[str, Any]],
    route: str,
    provenance: dict[str, Any],
    user_name: str,
    kind: str = "answer",
    source_run_id: int | None = None,
    audit: tuple[str, str] | None = None,
) -> int:
    """Write one question/answer turn once, re-checking access at commit time.

    ``source_run_id`` makes Agent turns idempotent: a resumed or retried run
    cannot append the same answer twice.
    """
    if kind not in TURN_KINDS:
        raise ValueError(f"未知的轮次类型：{kind}")
    stripped_question = question.strip()
    if not stripped_question:
        raise ValueError("问题不能为空")
    ts = now()
    with transaction() as conn:
        if conversation_id is not None:
            row = conn.execute(
                "SELECT id, case_id, archived_at FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None or int(row["case_id"]) != int(case_id):
                raise ConversationNotFoundError("会话不存在或不属于当前案件")
            if row["archived_at"] is not None:
                raise ArchivedConversationError("该会话已归档，只能查看历史记录；如需继续提问，请新建会话")
        else:
            conversation_id = conn.execute(
                "INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (?, ?, ?, ?)",
                (case_id, user_name or "本机律师", _title(stripped_question), ts),
            ).lastrowid
        if source_run_id is not None:
            existing = conn.execute(
                "SELECT conversation_id FROM messages WHERE source_run_id=?", (source_run_id,)
            ).fetchone()
            if existing is not None:
                return int(existing["conversation_id"])
        conn.execute(
            """INSERT INTO messages(conversation_id, role, content, citations_json, route, kind, created_at)
               VALUES (?, 'user', ?, '[]', ?, 'answer', ?)""",
            (conversation_id, stripped_question, route, ts),
        )
        conn.execute(
            """INSERT INTO messages(conversation_id, role, content, citations_json, route, provenance_json,
                                    kind, source_run_id, created_at)
               VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                answer,
                json.dumps(citations, ensure_ascii=False),
                route,
                json.dumps(provenance, ensure_ascii=False),
                kind,
                source_run_id,
                ts,
            ),
        )
        if audit is not None:
            action, detail = audit
            conn.execute(
                "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                (case_id, action, detail, ts),
            )
    return int(conversation_id)
