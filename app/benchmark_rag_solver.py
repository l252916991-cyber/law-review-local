"""Opt-in, gold-blind statutory RAG strategy using the same local model client."""
from __future__ import annotations

import time
from typing import Any

from app.benchmark_retrieval import retrieve
from app.benchmark_postprocess import postprocess
from app.benchmark_solver import (
    DIRECT_SYSTEM, GUIDED_SYSTEM_SUFFIX, SOLVER_VERSION, TASK_GUIDANCE,
    _call, _configuration, solve as base_solve,
)


def solve(task_id: str, instruction: str, question: str, config: dict[str, Any]) -> dict[str, Any]:
    """Exactly one call; absent context uses the unchanged task-guided strategy.

    Context is a separate user message, never a system instruction. Retrieval
    details are recorded even when no hits are found. No reference is accepted.
    """
    started = time.monotonic()
    result: dict[str, Any] = {"prediction": "", "error": None, "latency_ms": 0.0, "finish_reason": None,
                              "usage": None, "calls": [], "model_config": {},
                              "solver_version": SOLVER_VERSION + "+statutory-rag-v1"}
    try:
        if config.get("strategy") != "statutory_rag":
            raise ValueError("This solver only accepts statutory_rag")
        directories = config.get("corpus_directories")
        if not isinstance(directories, list) or not directories or not all(isinstance(path, str) for path in directories):
            raise ValueError("corpus_directories must be a nonempty list of paths")
        if task_id not in TASK_GUIDANCE or not isinstance(instruction, str) or not instruction.strip() or not isinstance(question, str) or not question.strip():
            raise ValueError("Invalid task, instruction or question")
        effective = _configuration({**config, "strategy": "task_guided"})
        context = retrieve(task_id, question, directories)
        result["retrieval"] = context
        result["model_config"] = {**effective, "strategy": "statutory_rag", "corpus_directories": directories}
        if not context["context"]:
            fallback = base_solve(task_id, instruction, question, effective)
            result.update({key: value for key, value in fallback.items() if key not in {"solver_version", "model_config"}})
        else:
            messages = [
                {"role": "system", "content": DIRECT_SYSTEM + GUIDED_SYSTEM_SUFFIX + "\n本任务核对方法：" + TASK_GUIDANCE[task_id]},
                {"role": "user", "content": instruction.strip() + "\n" + question},
                {"role": "user", "content": context["context"]},
            ]
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
