"""Promoted strategies for four weak tasks; inputs contain no references."""
from __future__ import annotations

from typing import Any

from app.benchmark_solver import solve as base_solve
from app.benchmark_rag_solver import solve as rag_solve

RAG_TASKS = {"1-1", "2-1", "3-2"}


def solve(task_id: str, instruction: str, question: str, config: dict[str, Any]) -> dict[str, Any]:
    """Route only independently retained strategies; keep other tasks unchanged."""
    if task_id in RAG_TASKS:
        return rag_solve(task_id, instruction, question, {**config, "strategy": "statutory_rag"})
    return base_solve(task_id, instruction, question, {**config, "strategy": "task_guided"})
