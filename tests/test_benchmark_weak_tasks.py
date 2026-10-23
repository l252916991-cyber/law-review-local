from unittest.mock import patch

from app.benchmark_weak_tasks import solve


def test_retained_tasks_use_statutory_rag():
    with patch("app.benchmark_weak_tasks.rag_solve", return_value={"prediction": "A"}) as rag:
        for task in ("1-1", "2-1", "3-2"):
            result = solve(task, "说明", "问题", {"corpus_directories": ["corpus"], "reference": "GOLD_CANARY"})
            assert result == {"prediction": "A"}
            assert rag.call_args.args[3]["strategy"] == "statutory_rag"
            assert rag.call_args.args[3]["corpus_directories"] == ["corpus"]


def test_consultation_and_other_tasks_keep_guided_solver():
    with patch("app.benchmark_weak_tasks.base_solve", return_value={"prediction": "A"}) as base:
        for task in ("3-8", "1-2"):
            assert solve(task, "说明", "问题", {"reference": "GOLD_CANARY"}) == {"prediction": "A"}
            assert base.call_args.args[3]["strategy"] == "task_guided"
