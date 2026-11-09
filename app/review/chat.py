"""Chat turn orchestration: clarification and refusal notices, statistics answers, the model
answer with its contract check and fallback, citation cards and turn recording."""
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from app.core.config import LLMConfig
from app.core.text import concise
from app.retrieval.search import detect_route, expand_semantics, statistics_answer
from app.review.context import (
    build_clarification,
    build_insufficient,
    case_documents,
    context_policy,
    limit_contexts,
    load_history,
    memory_meta,
    object_note,
    prepare_memory_clues,
    record_turn_with_id,
    render_history,
    resolve_question,
    unsupported_clause_topic,
    validate_context_notice,
    validate_conversation,
)
from app.review.generation import (
    _account_llm_call,
    _active_turn_key,
    _last_llm_provenance,
    _last_llm_usage,
    call_local_llm,
    llm_provenance,
)

logger = logging.getLogger(__name__)


def tool_loop_enabled() -> bool:
    """Feature flag: native tool calling is opt-in per deployment."""
    from app.review.tool_loop import tool_loop_enabled as _enabled

    return _enabled()


def fallback_answer(question: str, route: str, contexts: list[dict[str, Any]], scope_label: str = "") -> str:
    if not contexts:
        return "当前案件材料中没有检索到足够依据。建议补充关键词或先完善内容级目录，请律师复核。"
    prefix = f"本轮仅检索{scope_label}。\n\n" if scope_label else ""
    if route == "多文档对比":
        grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for index, item in enumerate(contexts, 1):
            grouped[item["name"]].append((index, item))
        lines = ["已按不同文件分别检索，发现以下可比对内容："]
        for name, items in list(grouped.items())[:5]:
            index, item = items[0]
            lines.append(f"- **{name}**：{item['quote']}（第{item['page_no']}页）[资料{index}]")
        lines.append("\n请律师结合原页核验陈述主体、时间和语境，系统不自动替代质证判断。")
        return prefix + "\n".join(lines)
    lines = ["根据当前卷宗，相关材料显示："]
    for index, item in enumerate(contexts[:4], 1):
        lines.append(f"- {item['quote']} [资料{index}]")
    lines.append("\n以上为检索摘要，请律师点击资料卡返回原页复核关键结论。")
    return prefix + "\n".join(lines)


def _request_payload(result: dict[str, Any]) -> Callable[[int, int | None], dict[str, Any]]:
    """Replay payload factory: the commit adds the ids it has just generated."""

    def build(conversation_id: int, message_id: int | None) -> dict[str, Any]:
        return {**result, "conversation_id": conversation_id, "message_id": message_id}

    return build


def _record_notice_turn(
    *,
    case_id: int,
    conversation_id: int | None,
    question: str,
    answer: str,
    answer_status: str,
    notice: dict[str, Any],
    route: str,
    provenance: dict[str, Any],
    validation: dict[str, Any],
    retrieval_metrics: dict[str, Any],
    context_meta: dict[str, Any],
    user_name: str,
    audit: tuple[str, str],
    should_cancel: Callable[[], bool] | None,
    request_key: tuple[int, str] | None,
) -> dict[str, Any]:
    """Persist a clarification or insufficient-evidence turn; no model was called."""
    result = {
        "conversation_id": conversation_id,
        "answer": answer,
        "answer_status": answer_status,
        "clarification": notice,
        "route": route,
        "semantic_expansion": expand_semantics(question),
        "citations": [],
        "llm_used": False,
        "provenance": provenance,
        "validation": validation,
        "citation_check": "not_applicable",
        "rejected_llm_validation": None,
        "fallback_reason": None,
        "failure_diagnostic": None,
        "retrieval_metrics": retrieval_metrics,
        "context": context_meta,
    }
    turn = record_turn_with_id(
        case_id, conversation_id, question=question, answer=answer, citations=[],
        route=route, provenance=provenance, user_name=user_name or "本机律师",
        kind=answer_status, audit=audit, should_cancel=should_cancel,
        request_key=request_key,
        request_result=_request_payload(result) if request_key is not None else None,
    )
    result["conversation_id"] = turn.conversation_id
    result["message_id"] = turn.message_id
    return result


def _statistics_turn(case_id: int, question: str) -> tuple[str, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Deterministic catalogue statistics: (answer, contexts, validation, retrieval_metrics)."""
    answer, contexts = statistics_answer(case_id, question)
    answer += "\n\n以上为当前数据库目录统计，请律师核对材料是否完整。"
    validation = {
        "valid": True, "scope": "database_metadata_counts",
        "checks": {"metadata_query_succeeded": True}, "issues": [],
        "semantic_entailment_checked": False, "amount_calculations_checked": False,
        "lawyer_review_required": True,
    }
    retrieval_metrics = {
        "retrieval_mode": "database_statistics",
        "strategy": "deterministic_metadata_query",
        "source_count": len(contexts),
        "degraded": False,
    }
    return answer, contexts, validation, retrieval_metrics


def _generate_model_answer(
    *,
    case_id: int,
    question: str,
    route: str,
    contexts: list[dict[str, Any]],
    conversation_id: int | None,
    use_remote_embeddings: bool,
    history_text: str,
    memory_text: str,
    note: str,
    context_meta: dict[str, Any],
    on_token: Callable[[str], None] | None,
    phase: Callable[[str], None],
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Model answer that must pass the citation contract: (answer, provenance, rejected_validation).

    Tries the optional tool loop (non-streaming only), then a single-shot call, and
    at most one fallback model whose answer must pass the very same contract.
    Raises RuntimeError when no candidate passes; the caller does not save a turn.
    """
    # Local import avoids the services -> agents -> services import cycle.
    from app.review.agents import failure_diagnostic, validate_review_answer

    rejected_validation = None
    llm_started = time.perf_counter()
    tool_loop_used = False
    try:
        # The streaming path is single-shot: tokens arrive before the tool
        # loop could run, so combining them would publish unretractable text.
        if on_token is None and tool_loop_enabled():
            from app.review.tool_loop import ToolsUnsupportedError, run_tool_loop

            try:
                loop_result = run_tool_loop(
                    case_id=case_id,
                    question=question,
                    route=route,
                    contexts=contexts,
                    conversation_id=conversation_id,
                    use_remote_embeddings=use_remote_embeddings,
                    history=history_text,
                    memory=memory_text,
                    object_note=note,
                )
            except ToolsUnsupportedError:
                logger.info("Endpoint rejected tools; using single-shot answer")
                loop_result = None
            if loop_result is not None:
                tool_loop_used = True
                answer = loop_result["answer"]
                context_meta["tools"] = {
                    "rounds": loop_result["rounds"],
                    "tool_calls": loop_result["tool_calls"],
                    "trace": loop_result["tool_trace"],
                    "model": loop_result["model"],
                }
        if not tool_loop_used:
            answer = call_local_llm(
                question, route, contexts, history=history_text,
                memory=memory_text, object_note=note, on_token=on_token,
            )
        phase("validating")
        candidate_validation = validate_review_answer(answer, contexts)
        route_note = None
        if not candidate_validation["valid"]:
            # One bounded fallback attempt. The replacement answer must
            # pass the very same contract; a fallback is not an exemption.
            from app.llm.model_router import fallback_target

            target = fallback_target()
            if target is not None:
                fallback_started = time.perf_counter()
                # Never name this ``fallback_answer``: that is the
                # module-level rule-summary function used by the caller.
                retry_answer = call_local_llm(
                    question, route, contexts, history=history_text,
                    memory=memory_text, object_note=note,
                    model_override=target.model, base_url_override=target.base_url,
                    api_override=target.api,
                )
                fallback_validation = validate_review_answer(retry_answer, contexts)
                if fallback_validation["valid"]:
                    _account_llm_call(
                        case_id, "chat", llm_started, "failed", conversation_id=conversation_id,
                    )
                    _account_llm_call(
                        case_id, "chat_fallback", fallback_started, "ok",
                        conversation_id=conversation_id,
                    )
                    rejected_validation = candidate_validation
                    answer, candidate_validation = retry_answer, fallback_validation
                    route_note = {
                        "fallback": True,
                        "reason": "primary_failed_validation",
                        "model": target.model,
                    }
        if not candidate_validation["valid"]:
            logger.warning("Chat validation failed issues=%s", ",".join(candidate_validation["issues"]))
            raise RuntimeError("model_output_failed_validation")
        provenance = dict(_last_llm_provenance.get() or llm_provenance(route, LLMConfig.load().model))
        if route_note:
            provenance["route"] = route_note
        if not tool_loop_used and not route_note:
            _account_llm_call(
                case_id, "chat", llm_started, "ok", conversation_id=conversation_id,
            )
        return answer, provenance, rejected_validation
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        if not tool_loop_used:
            _account_llm_call(
                case_id, "chat", llm_started, "failed", conversation_id=conversation_id,
            )
        failure_diagnostic(exc, "chat_llm")
        logger.warning("Chat model failed error_type=%s", type(exc).__name__)
        raise RuntimeError(f"model_unavailable_or_invalid:{type(exc).__name__}") from exc


def _citation_cards(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clickable citation cards, one-to-one with the first six prompt contexts."""
    return [
        {
            "index": i,
            "document_id": item["document_id"],
            "document_name": item["name"],
            "page": item["page_no"],
            "quote": item["quote"],
            "url": f"/api/documents/{item['document_id']}/file#page={item['page_no']}",
        }
        for i, item in enumerate(contexts[:6], 1)
    ]


def chat(
    case_id: int,
    question: str,
    user_name: str,
    conversation_id: int | None,
    use_llm: bool,
    use_remote_embeddings: bool = True,
    on_token: Callable[[str], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    request_key: tuple[int, str] | None = None,
) -> dict[str, Any]:
    # Local import avoids the services -> agents -> services import cycle.
    from app.review.agents import validate_review_answer

    def phase(name: str) -> None:
        if on_phase is not None:
            on_phase(name)

    def check_cancelled() -> None:
        if should_cancel is not None and should_cancel():
            from app.review.chat_idempotency import ChatCancelled

            raise ChatCancelled("客户端已断开，停止本轮后续处理")

    # Per-turn observation: a request context reused by the test client (or a
    # worker context copied from it) must not report the previous turn's usage.
    _last_llm_usage.set(None)
    # One logical-question key for every call this turn: tool rounds, primary and
    # fallback model calls all land under it. A retried request reuses its id.
    _active_turn_key.set(
        f"request:{request_key[1]}" if request_key is not None
        else f"turn-{uuid.uuid4().hex[:16]}"
    )
    question = question.strip()
    if not question:
        raise ValueError("问题不能为空")
    # An explicitly supplied conversation must be validated before history,
    # retrieval or a model call: unknown, cross-case and archived sessions are
    # rejected instead of silently becoming a new conversation.
    policy = context_policy()
    if conversation_id is not None:
        validate_conversation(case_id, conversation_id)
    history = load_history(case_id, conversation_id, policy=policy)
    documents = case_documents(case_id)
    documents_by_id = {document["id"]: document for document in documents}
    resolution = resolve_question(case_id, question, history, documents=documents, policy=policy)
    resolution_meta = resolution.as_dict(documents_by_id)
    context_meta = {
        "policy": policy.as_dict(),
        "history": history.as_dict(),
        "resolution": resolution_meta,
        "memory": memory_meta(policy, reason="not_started"),
    }
    audit = ("AI阅卷问答", f"{user_name or '本机律师'}：{concise(question, 60)}")
    notice_turn = {
        "case_id": case_id, "conversation_id": conversation_id, "question": question,
        "context_meta": context_meta, "user_name": user_name, "audit": audit,
        "should_cancel": should_cancel, "request_key": request_key,
    }

    if resolution.status == "needs_clarification":
        context_meta["memory"] = memory_meta(policy, reason="clarification_required")
        answer, clarification = build_clarification(question, resolution, documents_by_id)
        return _record_notice_turn(
            **notice_turn, answer=answer, answer_status="clarification", notice=clarification,
            route="需要澄清",
            provenance={
                "mode": "clarification-request",
                "llm_attempted": False,
                "context_policy": policy.version,
                "resolution": resolution_meta,
                "clarification": clarification,
            },
            validation=validate_context_notice(answer, "clarification"),
            retrieval_metrics={"retrieval_mode": "not_started", "source_count": 0},
        )

    route = detect_route(resolution.retrieval_query)
    fallback_reason, diagnostic, rejected_validation = None, None, None
    history_text = (
        render_history(history.turns)
        if resolution.status == "resolved" and history.enabled and history.turns
        else ""
    )
    if route == "目录统计":
        context_meta["memory"] = memory_meta(policy, reason="route_not_model")
        answer, contexts, validation, retrieval_metrics = _statistics_turn(case_id, question)
        llm_used = False
        provenance = {"mode": "database-statistics"}
        citation_check = "not_applicable"
    else:
        # Keep the prompt context and the clickable citation cards one-to-one.
        from app.retrieval.rag import HybridRetriever

        contexts, retrieval_metrics = HybridRetriever(
            case_id, prefer_remote_embeddings=use_remote_embeddings
        ).retrieve(resolution.retrieval_query, 6)
        contexts = limit_contexts(contexts, resolution.object_document_ids)
        missing_topic = unsupported_clause_topic(case_id, question, contexts) if contexts else ""
        if not contexts or missing_topic:
            context_meta["memory"] = memory_meta(policy, reason="insufficient_source")
            answer, insufficient = build_insufficient(
                question, resolution, documents_by_id,
                reason="asked_topic_not_in_current_sources" if missing_topic else "no_current_supporting_source",
            )
            context_meta["history"] = history.as_dict(used=False)
            return _record_notice_turn(
                **notice_turn, answer=answer, answer_status="insufficient_evidence", notice=insufficient,
                route="资料不足",
                provenance={
                    "mode": "insufficient-evidence",
                    "llm_attempted": False,
                    "context_policy": policy.version,
                    "resolution": resolution_meta,
                    "clarification": insufficient,
                },
                validation=validate_context_notice(answer, "insufficient_evidence"),
                retrieval_metrics=retrieval_metrics,
            )
        note = object_note(resolution, documents_by_id)
        scope_label = "、".join(
            f"《{documents_by_id[document_id]['name']}》"
            for document_id in resolution.object_document_ids if document_id in documents_by_id
        )
        llm_used = False
        if use_llm:
            memory_clues = prepare_memory_clues(
                case_id, resolution.retrieval_query, use_remote_embeddings, policy,
                require_history=bool(history_text),
            )
            context_meta["memory"] = memory_clues["meta"]
            # A disconnect noticed before the model call saves the compute; the
            # call already in flight is never interrupted, only the commit is.
            check_cancelled()
            phase("generating")
            answer, provenance, rejected_validation = _generate_model_answer(
                case_id=case_id, question=question, route=route, contexts=contexts,
                conversation_id=conversation_id, use_remote_embeddings=use_remote_embeddings,
                history_text=history_text, memory_text=memory_clues["text"], note=note,
                context_meta=context_meta, on_token=on_token, phase=phase,
            )
            llm_used = True
        else:
            context_meta["memory"] = memory_meta(policy, reason="llm_disabled")
            answer = fallback_answer(question, route, contexts, scope_label)
            provenance = {"mode": "rule-retrieval", "llm_attempted": False}
        if fallback_reason:
            answer += f"\n\n> {fallback_reason}。"
        validation = validate_review_answer(answer, contexts)
        citation_check = "passed" if validation["valid"] else "failed"

    provenance = {
        **provenance,
        "context": {
            "policy_version": policy.version,
            "history_used": bool(history_text),
            "history_digest": history.digest,
            "resolution": resolution_meta,
            "memory": context_meta["memory"],
        },
    }
    context_meta["history"] = history.as_dict(used=bool(history_text))
    citations = _citation_cards(contexts)
    result = {
        "conversation_id": conversation_id,
        "answer": answer,
        "answer_status": "answered",
        "clarification": None,
        "route": route,
        "semantic_expansion": expand_semantics(question),
        "citations": citations,
        "llm_used": llm_used,
        "provenance": provenance,
        "validation": validation,
        "citation_check": citation_check,
        "rejected_llm_validation": rejected_validation,
        "fallback_reason": fallback_reason,
        "failure_diagnostic": diagnostic,
        "retrieval_metrics": retrieval_metrics,
        "context": context_meta,
    }
    turn = record_turn_with_id(
        case_id,
        conversation_id,
        question=question,
        answer=answer,
        citations=citations,
        route=route,
        provenance=provenance,
        user_name=user_name or "本机律师",
        audit=audit,
        should_cancel=should_cancel,
        request_key=request_key,
        request_result=_request_payload(result) if request_key is not None else None,
    )
    result["conversation_id"] = turn.conversation_id
    result["message_id"] = turn.message_id
    return result
