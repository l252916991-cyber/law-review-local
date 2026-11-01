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
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .chat_idempotency import ChatCancelled
from .chat_idempotency import commit as commit_request
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
CITATION_MARKER = re.compile(r"[\[【［]\s*资料\s*\d+\s*[\]】］]")

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
    clarification_options: tuple[dict[str, Any], ...] = ()

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


def _parse_clarification_options(row: Any) -> tuple[dict[str, Any], ...]:
    try:
        provenance = json.loads(_row_get(row, "provenance_json", "{}") or "{}")
    except (TypeError, ValueError):
        return ()
    clarification = provenance.get("clarification") if isinstance(provenance, dict) else None
    options = clarification.get("options") if isinstance(clarification, dict) else None
    if not isinstance(options, list):
        return ()
    parsed: list[dict[str, Any]] = []
    for item in options:
        if not isinstance(item, dict):
            continue
        try:
            document_id = int(item.get("document_id"))
        except (TypeError, ValueError):
            continue
        parsed.append({"document_id": document_id, "name": str(item.get("name") or "")})
    return tuple(parsed)


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
            """SELECT id, role, content, citations_json, route, kind, provenance_json
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
                    kind=kind,
                    citations=citations,
                    clarification_options=_parse_clarification_options(row),
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
        assistant_content = CITATION_MARKER.sub("（历史引用编号已移除）", turn.assistant_content)
        lines.append(f"[第{index}轮] 助手（{marker}）：{assistant_content}")
    return "\n".join(lines)


def _title(question: str, limit: int = 30) -> str:
    text = re.sub(r"\s+", " ", question).strip()
    if not text:
        return "新会话"
    return text[:limit]


@dataclass(frozen=True)
class TurnRecord:
    """Identifiers of the committed turn; ``message_id`` stays ``None`` for replays."""

    conversation_id: int
    message_id: int | None = None


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
    request_key: tuple[int, str] | None = None,
    request_result: Callable[[int, int | None], dict[str, Any]] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> int:
    """Write one turn and return its conversation id (historical contract)."""
    return record_turn_with_id(
        case_id, conversation_id, question=question, answer=answer, citations=citations,
        route=route, provenance=provenance, user_name=user_name, kind=kind,
        source_run_id=source_run_id, audit=audit, request_key=request_key,
        request_result=request_result, should_cancel=should_cancel,
    ).conversation_id


def record_turn_with_id(
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
    request_key: tuple[int, str] | None = None,
    request_result: Callable[[int, int | None], dict[str, Any]] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> TurnRecord:
    """Write one question/answer turn once, re-checking access at commit time.

    ``source_run_id`` makes Agent turns idempotent: a resumed or retried run
    cannot append the same answer twice. ``request_key`` extends that guarantee
    to a client-supplied request id for the streaming chat path: the idempotency
    row is updated in this same transaction, so a retry either replays the whole
    turn or starts from nothing. ``should_cancel`` is consulted before the
    transaction opens; a disconnect that lands after this point loses the race
    on purpose (the commit wins and the retry replays).
    """
    if kind not in TURN_KINDS:
        raise ValueError(f"未知的轮次类型：{kind}")
    stripped_question = question.strip()
    if not stripped_question:
        raise ValueError("问题不能为空")
    if should_cancel is not None and should_cancel():
        raise ChatCancelled("客户端已断开，未写入本轮问答")
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
                "SELECT id, conversation_id FROM messages WHERE source_run_id=?", (source_run_id,)
            ).fetchone()
            if existing is not None:
                return TurnRecord(int(existing["conversation_id"]), int(existing["id"]))
        conn.execute(
            """INSERT INTO messages(conversation_id, role, content, citations_json, route, kind, created_at)
               VALUES (?, 'user', ?, '[]', ?, 'answer', ?)""",
            (conversation_id, stripped_question, route, ts),
        )
        answer_cursor = conn.execute(
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
        message_id = int(answer_cursor.lastrowid) if answer_cursor.lastrowid else None
        if request_key is not None and request_result is not None:
            commit_request(
                conn, int(request_key[0]), str(request_key[1]),
                conversation_id=int(conversation_id), message_id=message_id,
                result=request_result(int(conversation_id), message_id),
            )
    return TurnRecord(int(conversation_id), message_id)


# ---------------------------------------------------------------------------
# Object resolution and non-factual answer contracts (M03)
# ---------------------------------------------------------------------------

ANAPHORA_PATTERN = re.compile(
    r"(?:[它他她](?:们)?"
    r"|(?:该|此)(?:合|协|文|材|份|笔|案|项|条|款|公|银|集|期|金|数|地|时|单位)"
    r"|这(?:份|个|件|家|笔|条|些|项|批|款)"
    r"|那(?:份|个|件|家|笔|条|些|项|批|款)"
    r"|(?:上述|前述|前面|刚才|上面|此前)"
    r"|同一(?:份|个|笔|家|项|条)?"
    r"|其(?![他余中次实间它项]))"
)
CORRECTION_PATTERN = re.compile(r"(?:不是|并非|不对|改看|改为|换成|换一份|另一份|别看|更正为|重新看)")
TYPE_WORDS = (
    "补充协议", "采购合同", "框架协议", "会议纪要", "起诉意见书", "银行流水", "对账单",
    "合同", "协议", "备忘录", "确认书", "订单", "发票", "笔录", "电子邮件", "宣传材料",
    "报告", "说明", "清单", "凭证", "流水", "纪要",
)
# Closed set of specific clause words used only for a conservative "asked but
# absent" gate. Generic words (付款/期限/交付) are deliberately not included.
CLAUSE_TOPIC_TERMS = (
    "保密", "违约金", "竞业", "保修", "质保", "不可抗力", "仲裁", "管辖",
    "解除", "终止", "送达", "发票", "税费",
)
ENTITY_SUFFIXES = ("有限责任公司", "股份有限公司", "有限公司", "公司", "集团", "银行", "事务所", "中心", "工作室")
_NAME_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,6}$")
_NAME_PREFIX = re.compile(r"^\s*(?:[0-9]+|[一二三四五六七八九十]+)\s*[_\-\.．、)]\s*")


@dataclass(frozen=True)
class ResolvedQuestion:
    original: str
    retrieval_query: str
    status: str
    method: str
    reason: str
    object_document_ids: tuple[int, ...] = ()
    basis_message_ids: tuple[int, ...] = ()
    options: tuple[dict[str, Any], ...] = ()
    correction: bool = False
    policy_version: str = CONTEXT_POLICY_VERSION
    history_digest: str = ""

    @property
    def needs_clarification(self) -> bool:
        return self.status == "needs_clarification"

    def as_dict(self, documents: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
        documents = documents or {}
        return {
            "status": self.status,
            "method": self.method,
            "reason": self.reason,
            "original": self.original,
            "retrieval_query": self.retrieval_query,
            "object_document_ids": list(self.object_document_ids),
            "object_document_names": [
                documents[doc_id]["name"] for doc_id in self.object_document_ids if doc_id in documents
            ],
            "basis_message_ids": list(self.basis_message_ids),
            "options": [dict(option) for option in self.options],
            "correction": self.correction,
            "policy_version": self.policy_version,
            "history_digest": self.history_digest,
        }


def case_documents(case_id: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, name, doc_type FROM documents WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
    finally:
        conn.close()
    return [{"id": int(row["id"]), "name": str(row["name"] or ""), "doc_type": str(row["doc_type"] or "")} for row in rows]


def _document_stem(name: str) -> str:
    stem = _NAME_EXTENSION.sub("", name or "")
    return _NAME_PREFIX.sub("", stem).strip()


def _document_name_keys(name: str) -> list[str]:
    stem = _NAME_EXTENSION.sub("", name or "")
    cleaned = _NAME_PREFIX.sub("", stem).strip()
    keys = {name.strip(), stem.strip(), cleaned}
    for part in re.split(r"[_\-\s.．、（）()【】\[\]]+", cleaned):
        if len(part) >= 4:
            keys.add(part)
    return sorted((key for key in keys if len(key) >= 4), key=len, reverse=True)


def _entity_tokens(documents: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for document in documents:
        stem = _document_stem(document["name"])
        for suffix in ENTITY_SUFFIXES:
            start = 0
            while True:
                index = stem.find(suffix, start)
                if index < 0:
                    break
                prefix = re.search(r"([\u4e00-\u9fffA-Za-z]{1,6})$", stem[:index])
                if prefix:
                    tokens.add(prefix.group(1) + suffix)
                start = index + len(suffix)
    return tokens


def _type_tokens(question: str) -> list[str]:
    chosen: list[str] = []
    for word in sorted(TYPE_WORDS, key=len, reverse=True):
        if word in question and not any(word in existing for existing in chosen):
            chosen.append(word)
    return chosen


def _explicit_matches(question: str, documents: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any], str]]:
    matches: dict[int, tuple[int, dict[str, Any], str]] = {}
    for document in documents:
        for key in _document_name_keys(document["name"]):
            position = question.find(key)
            if position >= 0:
                current = matches.get(document["id"])
                if current is None or position < current[0]:
                    matches[document["id"]] = (position, document, "explicit_name")
                break
    question_entities = [token for token in _entity_tokens(documents) if token in question]
    types = _type_tokens(question)
    if question_entities or types:
        for document in documents:
            if document["id"] in matches:
                continue
            name = document["name"]
            if question_entities and not any(token in name for token in question_entities):
                continue
            if types and not any(token in name for token in types):
                continue
            if not question_entities and not types:
                continue
            first_position = min(
                [question.find(token) for token in [*question_entities, *types] if token in question] or [len(question)]
            )
            method = "explicit_entity_type" if question_entities else "explicit_document_type"
            matches[document["id"]] = (first_position, document, method)
    return sorted(matches.values(), key=lambda item: (item[0], item[1]["id"]))


def _match_documents(matches: list[tuple[int, dict[str, Any], str]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    documents: list[dict[str, Any]] = []
    for _, document, _ in matches:
        if document["id"] not in seen:
            seen.add(document["id"])
            documents.append(document)
    return documents


def _option(document: dict[str, Any]) -> dict[str, Any]:
    return {"document_id": int(document["id"]), "name": str(document["name"])}


def _selection(question: str, options: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> dict[str, Any] | None:
    for option in options:
        name = str(option.get("name") or "")
        if name and (name in question or _document_stem(name) in question):
            return option
    match = re.search(r"第\s*([1-9])\s*(?:份|个|项|条)?", question)
    if match is None:
        match = re.fullmatch(r"\s*([1-9])\s*", question)
    if match is not None:
        index = int(match.group(1))
        if 1 <= index <= len(options):
            return options[index - 1]
    return None


def _history_object_candidates(
    history: HistorySnapshot, documents: dict[int, dict[str, Any]]
) -> tuple[list[int], tuple[int, ...], str]:
    for turn in reversed(history.turns):
        if not turn.factual:
            continue
        cited: list[int] = []
        for citation in turn.citations:
            try:
                document_id = int(citation.get("document_id"))
            except (TypeError, ValueError):
                continue
            if document_id in documents and document_id not in cited:
                cited.append(document_id)
        if cited:
            return cited, turn.message_ids, "history_citation"
        if turn.citations:
            # The turn cited material that no longer exists/is accessible. Do
            # not guess a replacement object from loose words in that turn.
            return [], (), "history_citation_deleted"
        mentioned = [document["id"] for _, document, _ in _explicit_matches(turn.user_content, list(documents.values()))]
        if mentioned:
            return mentioned, turn.message_ids, "history_mention"
    return [], (), ""


def _fallback_options(question: str, documents: list[dict[str, Any]], limit: int = 5) -> tuple[dict[str, Any], ...]:
    types = _type_tokens(question)
    ranked = [document for document in documents if any(token in document["name"] for token in types)]
    ranked.extend(document for document in documents if document not in ranked)
    return tuple(_option(document) for document in ranked[:limit])


def _last_clarification(history: HistorySnapshot) -> HistoryTurn | None:
    if history.turns and history.turns[-1].kind == "clarification":
        return history.turns[-1]
    return None


def resolve_question(
    case_id: int,
    question: str,
    history: HistorySnapshot | None = None,
    *,
    documents: list[dict[str, Any]] | None = None,
    policy: ContextPolicy | None = None,
) -> ResolvedQuestion:
    """Resolve what this turn refers to without inventing facts or objects."""
    policy = policy or context_policy()
    documents = case_documents(case_id) if documents is None else documents
    history = history or _empty_snapshot(case_id, None, policy)
    documents_by_id = {document["id"]: document for document in documents}
    question = question.strip()
    anaphora = bool(ANAPHORA_PATTERN.search(question))
    correction = bool(CORRECTION_PATTERN.search(question))

    def result(**kwargs: Any) -> ResolvedQuestion:
        base = {
            "original": question,
            "retrieval_query": question,
            "status": "explicit",
            "method": "none",
            "reason": "no_ellipsis",
            "policy_version": policy.version,
            "history_digest": history.digest,
            "correction": correction,
        }
        base.update(kwargs)
        return ResolvedQuestion(**base)

    explicit = _explicit_matches(question, documents)
    if explicit:
        matched = _match_documents(explicit)
        ids = tuple(document["id"] for document in matched)
        # A bare generic type word ("哪份合同") matching several documents is a
        # weak hint, not a specific indication. When the question is elliptical,
        # let the verifiable prior object win instead of asking a needless
        # clarification; an explicit correction still always clarifies.
        weak_multi = len(ids) > 1 and all(method == "explicit_document_type" for _, _, method in explicit)
        if weak_multi and anaphora and not correction:
            pass
        elif len(ids) > 1 and (anaphora or correction):
            return result(
                status="needs_clarification",
                method="ambiguous_explicit_object",
                reason="explicit_mention_matches_multiple_documents",
                options=tuple(_option(document) for document in matched),
            )
        else:
            return result(
                status="explicit",
                method=explicit[0][2],
                reason="explicit_correction" if correction else "explicit_object",
                object_document_ids=ids,
            )

    pending = _last_clarification(history) if policy.history_enabled else None
    if pending is not None and pending.clarification_options:
        selected = _selection(question, pending.clarification_options)
        if selected is not None and int(selected["document_id"]) in documents_by_id:
            document = documents_by_id[int(selected["document_id"])]
            return result(
                status="resolved",
                method="clarification_selection",
                reason="user_selected_previous_option",
                object_document_ids=(document["id"],),
                basis_message_ids=(pending.user_message_id, pending.assistant_message_id),
                retrieval_query=_resolved_query(question, [document]),
            )

    if anaphora or correction:
        if policy.history_enabled:
            candidate_ids, basis, method = _history_object_candidates(history, documents_by_id)
            candidates = [documents_by_id[document_id] for document_id in candidate_ids]
            if len(candidates) == 1:
                return result(
                    status="resolved",
                    method=method,
                    reason="single_prior_object",
                    object_document_ids=(candidates[0]["id"],),
                    basis_message_ids=basis,
                    retrieval_query=_resolved_query(question, candidates),
                )
            if len(candidates) > 1:
                return result(
                    status="needs_clarification",
                    method="ambiguous_history_object",
                    reason="multiple_prior_objects",
                    basis_message_ids=basis,
                    options=tuple(_option(document) for document in candidates),
                )
        if pending is not None and pending.clarification_options:
            options = tuple(
                option for option in pending.clarification_options
                if int(option["document_id"]) in documents_by_id
            )
            return result(
                status="needs_clarification",
                method="pending_clarification",
                reason="unselected_previous_clarification",
                basis_message_ids=(pending.user_message_id, pending.assistant_message_id),
                options=options or _fallback_options(question, documents),
            )
        return result(
            status="needs_clarification",
            method="no_prior_object",
            reason="no_prior_context" if not history.turns else "no_resolvable_object",
            options=_fallback_options(question, documents),
        )

    return result()


def _resolved_query(question: str, documents: list[dict[str, Any]]) -> str:
    names = [stem for stem in (_document_stem(document["name"]) for document in documents) if stem and stem not in question]
    return f"{question} {' '.join(names)}".strip() if names else question


def limit_contexts(
    contexts: list[dict[str, Any]], document_ids: tuple[int, ...] | list[int]
) -> list[dict[str, Any]]:
    """Keep retrieval inside the resolved objects; never silently widen it later."""
    if not document_ids:
        return contexts
    allowed = set(int(document_id) for document_id in document_ids)
    return [item for item in contexts if int(item.get("document_id") or 0) in allowed]


def object_note(resolution: ResolvedQuestion, documents: dict[int, dict[str, Any]]) -> str:
    if not resolution.object_document_ids:
        return ""
    names = "、".join(
        f"《{documents[document_id]['name']}》"
        for document_id in resolution.object_document_ids
        if document_id in documents
    )
    if not names:
        return ""
    if resolution.method == "clarification_selection":
        basis = "用户在本轮选择了此前澄清的选项"
    elif resolution.method.startswith("history"):
        basis = "根据本会话前文引用定位"
    elif resolution.correction:
        basis = "用户本轮明确更正了对象"
    else:
        basis = "用户本轮明确提及"
    return (
        f"本轮指代对象：{names}（{basis}；仅用于定位，事实必须由本轮卷宗片段支持）。"
        f"\n用于检索的问题：{resolution.retrieval_query}"
    )


def build_clarification(
    question: str, resolution: ResolvedQuestion, documents: dict[int, dict[str, Any]] | None = None
) -> tuple[str, dict[str, Any]]:
    documents = documents or {}
    options = [
        {"document_id": int(option["document_id"]), "name": str(option.get("name") or "")}
        for option in resolution.options
    ]
    if resolution.reason == "multiple_prior_objects":
        lead = "前文提到多份材料，当前问题无法唯一确定所指对象。"
    elif resolution.reason == "explicit_mention_matches_multiple_documents":
        lead = "本轮提到的材料名称对应多份材料，暂不能自行选定。"
    elif resolution.reason == "unselected_previous_clarification":
        lead = "上一轮澄清尚未选定对象。"
    else:
        lead = "当前会话没有可供指代的前文对象。"
    if options:
        listing = "；".join(f"{index}.《{option['name']}》" for index, option in enumerate(options, 1))
        answer = (
            f"{lead}请确认你指的是哪份材料：{listing}。"
            "请直接回复序号或材料名称；在对象确认前，系统不会猜测事实。"
        )
    else:
        answer = (
            f"{lead}当前案件没有可用于定位的材料。请先上传材料并说明所问对象；"
            "在对象确认前，系统不会猜测事实。"
        )
    payload = {
        "reason": resolution.reason,
        "question": question,
        "options": options,
        "message": answer,
    }
    return answer, payload


def unsupported_clause_topic(case_id: int, question: str, contexts: list[dict[str, Any]]) -> str:
    """Detect a rare asked-for clause whose term is absent from the actual pages.

    This is a narrow, fail-closed gate for a closed set of specific clause words
    (for example 保密/违约金). It does not attempt semantic entailment; when the
    term is absent from every page used this round, the honest result is
    "insufficient evidence", not a generic summary of unrelated pages.
    """
    for term in CLAUSE_TOPIC_TERMS:
        if term not in question:
            continue
        conn = connect()
        try:
            supported = False
            for item in contexts:
                document_id = item.get("document_id")
                page_no = item.get("page_no")
                if document_id is None or page_no is None:
                    continue
                row = conn.execute(
                    "SELECT text FROM pages WHERE document_id=? AND page_no=?",
                    (int(document_id), int(page_no)),
                ).fetchone()
                if row is not None and term in str(row["text"] or ""):
                    supported = True
                    break
        finally:
            conn.close()
        if not supported:
            return term
    return ""


def build_insufficient(
    question: str,
    resolution: ResolvedQuestion,
    documents: dict[int, dict[str, Any]] | None = None,
    *,
    reason: str = "no_current_supporting_source",
) -> tuple[str, dict[str, Any]]:
    documents = documents or {}
    scope = "、".join(
        f"《{documents[document_id]['name']}》"
        for document_id in resolution.object_document_ids
        if document_id in documents
    )
    scope_text = scope or "当前案件材料"
    answer = (
        f"在{scope_text}范围内，本轮检索没有找到支持所问事项的原文，资料不足。"
        "请补充材料、缩小问题范围或确认所指条款；系统不会引用历史回答或猜测事实。"
    )
    payload = {
        "reason": reason,
        "question": question,
        "scope_document_ids": list(resolution.object_document_ids),
        "message": answer,
    }
    return answer, payload


def validate_context_notice(answer: str, kind: str) -> dict[str, Any]:
    """Validate clarification/insufficient messages without relaxing fact answers."""
    text = answer or ""
    checks = {
        "non_empty": bool(text.strip()),
        "no_citation_markers": not bool(re.search(r"[\[【［]\s*资料", text)),
        "not_a_verified_answer": "请律师复核原文与上述结论" not in text,
        "asks_for_input": "请" in text,
    }
    if kind == "clarification":
        checks["confirmation_requested"] = "确认" in text or "说明" in text
    else:
        checks["insufficient_declared"] = "资料不足" in text
    issues = [name for name, passed in checks.items() if not passed]
    return {
        "valid": all(checks.values()),
        "scope": f"{kind}_message_no_factual_claims",
        "checks": checks,
        "issues": issues,
        "semantic_entailment_checked": False,
        "amount_calculations_checked": False,
        "lawyer_review_required": True,
    }


# ---------------------------------------------------------------------------
# Shared turn context for Agent runtimes, background jobs and resume (M04)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnContext:
    """One frozen turn prepared before any runtime executes."""

    case_id: int
    question: str
    conversation_id: int | None
    policy: ContextPolicy
    history: HistorySnapshot
    resolution: ResolvedQuestion
    documents_by_id: dict[int, dict[str, Any]]
    user_name: str = "本机律师"
    use_llm: bool = True

    @property
    def retrieval_question(self) -> str:
        return self.resolution.retrieval_query

    def identity(self, *, restored: bool = False) -> dict[str, Any]:
        history_text = (
            render_history(self.history.turns)
            if self.use_llm and self.resolution.status == "resolved" and self.history.enabled and self.history.turns
            else ""
        )
        return {
            "policy_version": self.policy.version,
            "policy": self.policy.as_dict(),
            "case_id": self.case_id,
            "question": self.question,
            "retrieval_question": self.retrieval_question,
            "conversation_id": self.conversation_id,
            "user_name": self.user_name,
            "history": {
                "enabled": self.history.enabled,
                "used": bool(history_text),
                "digest": self.history.digest,
                "cutoff_message_id": self.history.cutoff_message_id,
                "message_count": len(self.history.message_ids),
                "turns": len(self.history.turns),
                "chars": self.history.chars,
                "dropped_turns": self.history.dropped_turns,
                "truncated": self.history.truncated,
            },
            "history_text": history_text,
            "object_note": object_note(self.resolution, self.documents_by_id),
            "resolution": self.resolution.as_dict(self.documents_by_id),
            "restored": restored,
        }


def prepare_turn(
    case_id: int,
    question: str,
    conversation_id: int | None,
    *,
    user_name: str = "本机律师",
    policy: ContextPolicy | None = None,
    use_llm: bool = True,
) -> TurnContext:
    """Validate access, freeze history and resolve objects before any runtime."""
    question = question.strip()
    if not question:
        raise ValueError("问题不能为空")
    policy = policy or context_policy()
    if conversation_id is not None:
        validate_conversation(case_id, conversation_id)
    history = load_history(case_id, conversation_id, policy=policy)
    documents = case_documents(case_id)
    resolution = resolve_question(case_id, question, history, documents=documents, policy=policy)
    return TurnContext(
        case_id=case_id,
        question=question,
        conversation_id=conversation_id,
        policy=policy,
        history=history,
        resolution=resolution,
        documents_by_id={document["id"]: document for document in documents},
        user_name=user_name or "本机律师",
        use_llm=use_llm,
    )


def policy_from_identity(identity: dict[str, Any] | None) -> ContextPolicy:
    """Rebuild the frozen policy; unknown fields fall back to defaults."""
    data = (identity or {}).get("policy")
    if not isinstance(data, dict):
        return context_policy()
    defaults = ContextPolicy()
    return ContextPolicy(
        version=str(data.get("version") or defaults.version),
        history_enabled=bool(data.get("history_enabled", defaults.history_enabled)),
        max_history_turns=int(data.get("max_history_turns", defaults.max_history_turns)),
        max_history_chars=int(data.get("max_history_chars", defaults.max_history_chars)),
        max_turn_chars=int(data.get("max_turn_chars", defaults.max_turn_chars)),
        memory_enabled=bool(data.get("memory_enabled", defaults.memory_enabled)),
        max_memory_items=int(data.get("max_memory_items", defaults.max_memory_items)),
        max_memory_chars=int(data.get("max_memory_chars", defaults.max_memory_chars)),
        max_total_chars=int(data.get("max_total_chars", defaults.max_total_chars)),
    )


def context_result(identity: dict[str, Any] | None, *, restored: bool = False) -> dict[str, Any]:
    """Client/audit-facing metadata; never returns the frozen history text."""
    if not identity or not identity.get("policy_version"):
        return {
            "available": False,
            "restored": restored,
            "policy": None,
            "history": {"enabled": False, "used": False},
            "resolution": None,
            "conversation_id": None,
        }
    return {
        "available": True,
        "restored": restored,
        "policy_version": identity.get("policy_version"),
        "conversation_id": identity.get("conversation_id"),
        "history": dict(identity.get("history") or {}),
        "resolution": identity.get("resolution"),
    }


def run_context_identity(run_id: int) -> dict[str, Any]:
    """Read the frozen context snapshot recorded for one Agent run."""
    conn = connect()
    try:
        row = conn.execute("SELECT context_json FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    try:
        value = json.loads(row["context_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def validate_resume_identity(case_id: int, identity: dict[str, Any]) -> None:
    """Re-check the frozen conversation before a resumed run may continue.

    Old runs have no identity: they resume without context and are never
    retro-fitted with the current conversation.
    """
    if not identity or not identity.get("policy_version"):
        return
    conversation_id = identity.get("conversation_id")
    if conversation_id is None:
        return
    validate_conversation(case_id, int(conversation_id))
    history_meta = identity.get("history") or {}
    cutoff = int(history_meta.get("cutoff_message_id") or 0)
    expected = int(history_meta.get("message_count") or 0)
    if history_message_count(int(conversation_id), cutoff) != expected:
        raise ValueError("会话历史在运行期间发生变化，拒绝用新历史恢复旧运行")


def source_pages_exist(case_id: int, contexts: list[dict[str, Any]]) -> bool:
    """Whether every frozen context still points at a current case page."""
    if not contexts:
        return True
    conn = connect()
    try:
        for item in contexts:
            document_id = item.get("document_id")
            page_no = item.get("page_no")
            if document_id is None or page_no is None:
                return False
            found = conn.execute(
                """SELECT 1 FROM pages p JOIN documents d ON d.id=p.document_id
                   WHERE p.document_id=? AND p.page_no=? AND d.case_id=?""",
                (int(document_id), int(page_no), case_id),
            ).fetchone()
            if found is None:
                return False
        return True
    finally:
        conn.close()


def agent_clarification_result(
    context: TurnContext, *, runtime: str, total_ms: int = 0
) -> dict[str, Any]:
    """Non-factual Agent response when the object must be clarified first."""
    answer, clarification = build_clarification(
        context.question, context.resolution, context.documents_by_id
    )
    validation = validate_context_notice(answer, "clarification")
    identity = context.identity()
    return {
        "run_id": None,
        "answer": answer,
        "answer_status": "clarification",
        "clarification": clarification,
        "route": "需要澄清",
        "semantic_expansion": [],
        "agent_type": "LangGraph Multi-Agent" if runtime == "langgraph" else "DAG Multi-Agent",
        "plan": [],
        "steps": [],
        "tools_used": [],
        "citations": [],
        "retrieval_metrics": {"retrieval_mode": "not_started", "source_count": 0},
        "memory_hits": [],
        "llm_used": False,
        "llm_model": "",
        "fallback_reason": None,
        "validation": validation,
        "citation_check": "not_applicable",
        "failure_diagnostic": None,
        "memory_persisted": False,
        "total_ms": total_ms,
        "runtime": runtime,
        "checkpoint_thread_id": "",
        "resumable": False,
        "resume_count": 0,
        "conversation_id": context.conversation_id,
        "context": context_result(identity),
    }


def record_agent_turn(
    case_id: int,
    identity: dict[str, Any],
    result: dict[str, Any],
    *,
    source_run_id: int | None,
    audit_action: str = "Agent 阅卷",
    audit_detail: str = "",
) -> int | None:
    """Persist an Agent answer/clarification once, without inventing a context."""
    if not identity or not identity.get("policy_version"):
        # Legacy runs without a frozen snapshot must not create a new turn from
        # the current conversation.
        return None
    status = result.get("answer_status", "answered")
    kind = "answer" if status == "answered" else status
    provenance = {
        "mode": str(result.get("runtime") or "agent"),
        "run_id": result.get("run_id"),
        "answer_status": status,
        "context": result.get("context"),
        "llm_used": bool(result.get("llm_used")),
    }
    return record_turn(
        case_id,
        identity.get("conversation_id"),
        question=str(identity.get("question") or ""),
        answer=str(result.get("answer") or ""),
        citations=list(result.get("citations") or []),
        route=str(result.get("route") or ""),
        provenance=provenance,
        user_name=str(identity.get("user_name") or "本机律师"),
        kind=kind,
        source_run_id=source_run_id,
        audit=(audit_action, audit_detail) if audit_action else None,
    )


# ---------------------------------------------------------------------------
# Optional draft-memory clues (M05 ablation switch, default off)
# ---------------------------------------------------------------------------

MEMORY_TRUST = "structural_checks_only_lawyer_review_required"
MEMORY_HEADER = (
    "【历史草稿线索｜不可信，非证据；不得作为事实依据或引用来源，只可提示待核验方向】"
)


def _sanitize_clue(text: str) -> str:
    """A draft may quote an older citation token; strip it so it cannot be reused."""
    return CITATION_MARKER.sub("（历史引用编号已移除）", text).strip()


def _citation_pages_exist(conn: Any, case_id: int, citations: list[dict[str, Any]]) -> str:
    for citation in citations:
        document_id = citation.get("document_id")
        page_no = citation.get("page")
        if document_id is None or page_no is None:
            return "source_citation_invalid"
        found = conn.execute(
            """SELECT 1 FROM pages p JOIN documents d ON d.id=p.document_id
               WHERE p.document_id=? AND p.page_no=? AND d.case_id=?""",
            (int(document_id), int(page_no), case_id),
        ).fetchone()
        if found is None:
            return "source_pages_deleted"
    return ""


def valid_memory_clues(
    case_id: int, items: list[dict[str, Any]], policy: ContextPolicy
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only drafts whose source run and pages can still be verified.

    A draft never becomes evidence: this only decides whether it may be shown to
    the model as an untrusted lead. Unknown trust, missing runs, deleted source
    conversations, missing/stale page citations and budget overruns are excluded
    with a reason, never silently included.
    """
    valid: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    if not policy.memory_enabled or policy.max_memory_items <= 0 or policy.max_memory_chars <= 0:
        return valid, excluded
    used_chars = 0
    conn = connect()
    try:
        for item in items:
            memory_id = item.get("id")
            if len(valid) >= policy.max_memory_items:
                excluded.append({"memory_id": memory_id, "reason": "item_budget"})
                continue
            if item.get("trust") != MEMORY_TRUST:
                excluded.append({"memory_id": memory_id, "reason": "trust_marker_missing"})
                continue
            try:
                source_run_id = int(item.get("source_run_id"))
            except (TypeError, ValueError):
                excluded.append({"memory_id": memory_id, "reason": "source_run_missing"})
                continue
            run = conn.execute(
                """SELECT case_id, status, conversation_id, citations_json
                   FROM agent_runs WHERE id=?""",
                (source_run_id,),
            ).fetchone()
            if run is None or int(run["case_id"]) != int(case_id) or run["status"] != "completed":
                excluded.append({"memory_id": memory_id, "reason": "source_run_invalid"})
                continue
            if run["conversation_id"] is not None:
                conversation = conn.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (int(run["conversation_id"]),)
                ).fetchone()
                if conversation is None:
                    excluded.append({"memory_id": memory_id, "reason": "source_conversation_deleted"})
                    continue
            try:
                citations = json.loads(run["citations_json"] or "[]")
            except (TypeError, ValueError):
                citations = []
            citations = [entry for entry in citations if isinstance(entry, dict)] if isinstance(citations, list) else []
            if not citations:
                excluded.append({"memory_id": memory_id, "reason": "source_citation_missing"})
                continue
            citation_problem = _citation_pages_exist(conn, case_id, citations)
            if citation_problem:
                excluded.append({"memory_id": memory_id, "reason": citation_problem})
                continue
            content = _sanitize_clue(str(item.get("content") or ""))
            if not content:
                excluded.append({"memory_id": memory_id, "reason": "empty_content"})
                continue
            if used_chars + len(content) > policy.max_memory_chars:
                excluded.append({"memory_id": memory_id, "reason": "char_budget"})
                continue
            used_chars += len(content)
            valid.append({**item, "content": content})
    finally:
        conn.close()
    return valid, excluded


def render_memory(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = [MEMORY_HEADER]
    for item in items:
        lines.append(
            f"- [草稿#{item.get('id')}｜结构性校验草稿，需律师复核，非证据] {item['content']}"
        )
    return "\n".join(lines)


def memory_meta(
    policy: ContextPolicy,
    *,
    items: list[dict[str, Any]] | None = None,
    excluded: list[dict[str, Any]] | None = None,
    used: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    items = items or []
    excluded = excluded or []
    meta: dict[str, Any] = {
        "enabled": policy.memory_enabled,
        "used": used,
        "count": len(items),
        "items": [
            {
                "memory_id": item.get("id"),
                "source_run_id": item.get("source_run_id"),
                "trust": item.get("trust"),
            }
            for item in items
        ],
        "excluded": excluded,
        "max_items": policy.max_memory_items,
        "max_chars": policy.max_memory_chars,
    }
    if reason:
        meta["reason"] = reason
    return meta


def prepare_memory_clues(
    case_id: int,
    query: str,
    prefer_remote_embeddings: bool,
    policy: ContextPolicy,
    *,
    items: list[dict[str, Any]] | None = None,
    require_history: bool = True,
) -> dict[str, Any]:
    """Build optional untrusted draft clues; failures never break the answer."""
    if not policy.memory_enabled:
        return {"text": "", "meta": memory_meta(policy, reason="switch_off")}
    if require_history is False:
        return {"text": "", "meta": memory_meta(policy, reason="history_required")}
    if items is None:
        try:
            from .rag import recall_memories

            items = recall_memories(case_id, query, policy.max_memory_items, prefer_remote_embeddings)
        except Exception as exc:  # recall is optional; never fail the answer for a lead
            logger.warning("Draft memory recall failed error_type=%s", type(exc).__name__)
            return {"text": "", "meta": memory_meta(policy, reason="recall_failed")}
    valid, excluded = valid_memory_clues(case_id, list(items or []), policy)
    text = render_memory(valid)
    return {
        "text": text,
        "meta": memory_meta(policy, items=valid, excluded=excluded, used=bool(text)),
    }
