"""Opt-in, gold-blind statutory RAG strategy using the same local model client."""
from __future__ import annotations

import time
from typing import Any

from app.benchmark_retrieval import retrieve
from app.benchmark_postprocess import postprocess
from app.benchmark_solver import (
    DIRECT_SYSTEM, GUIDED_SYSTEM_SUFFIX, SOLVER_VERSION, TASK_GUIDANCE,
    _call, _configuration, solve as base_solve, task_guidance,
)


def messages_for(task_id: str, instruction: str, question: str, config: dict[str, Any],
                 context: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Return the exact statutory-RAG messages and effective solver configuration."""
    effective = _configuration({**config, "strategy": "task_guided"})
    if task_id not in TASK_GUIDANCE or not isinstance(instruction, str) or not instruction.strip() \
            or not isinstance(question, str) or not question.strip():
        raise ValueError("Invalid task, instruction or question")
    messages = [
        {"role": "system", "content": DIRECT_SYSTEM + GUIDED_SYSTEM_SUFFIX +
         "\n本任务核对方法：" + task_guidance(task_id, effective)},
        {"role": "user", "content": instruction.strip() + "\n" + question},
    ]
    if context.get("context"):
        messages.append({"role": "user", "content": context["context"]})
    return messages, effective


def solve(task_id: str, instruction: str, question: str, config: dict[str, Any]) -> dict[str, Any]:
    """Exactly one call; absent context uses the unchanged task-guided strategy.

    Context is a separate user message, never a system instruction. Retrieval
    details are recorded even when no hits are found. No reference is accepted.
    """
    started = time.monotonic()
    result: dict[str, Any] = {"prediction": "", "error": None, "latency_ms": 0.0, "finish_reason": None,
                              "usage": None, "calls": [], "model_config": {},
                              "solver_version": SOLVER_VERSION + "+statutory-rag-v2"}
    try:
        if config.get("strategy") != "statutory_rag":
            raise ValueError("This solver only accepts statutory_rag")
        directories = config.get("corpus_directories")
        if not isinstance(directories, list) or not directories or not all(isinstance(path, str) for path in directories):
            raise ValueError("corpus_directories must be a nonempty list of paths")
        ranker_policy = config.get("retrieval_ranker_policy", "auto")
        if ranker_policy not in {"auto", "lexical"}:
            raise ValueError("retrieval_ranker_policy must be auto or lexical")
        context = config.get("retrieval_context")
        if context is None:
            context = retrieve(task_id, question, directories, ranker_policy=ranker_policy)
        elif not isinstance(context, dict):
            raise ValueError("retrieval_context must be a retrieval result")
        messages, effective = messages_for(task_id, instruction, question, config, context)
        result["retrieval"] = context
        result["model_config"] = {**effective, "strategy": "statutory_rag", "corpus_directories": directories,
                                  "retrieval_ranker_policy": ranker_policy}
        if not context["context"]:
            fallback = base_solve(task_id, instruction, question, effective)
            result.update({key: value for key, value in fallback.items() if key not in {"solver_version", "model_config"}})
        else:
            call = _call(messages, effective, "statutory_rag")
            result["calls"].append(call)
            result.update({key: call[key] for key in ("prediction", "error", "finish_reason", "usage")})
        result["prediction"], result["postprocess"] = postprocess(
            task_id, question, result["prediction"], retrieval=context,
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["latency_ms"] = (time.monotonic() - started) * 1000
    return result
