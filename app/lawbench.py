"""Local access helpers for the official Chinese LawBench dataset."""

from __future__ import annotations

import json
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


LAW_BENCH_DIR = Path(__file__).resolve().parent.parent / "benchmarks" / "lawbench" / "zero_shot"
LAW_BENCH_SOURCE = "https://github.com/open-compass/LawBench"
LAW_BENCH_COMMIT = "e30981bb3ff54c41571f222e0b23e92d27375388"

TASK_NAMES = {
    "1-1": "法条背诵",
    "1-2": "司法考试知识问答",
    "2-1": "法律文件校对",
    "2-2": "纠纷焦点识别",
    "2-3": "婚姻纠纷鉴定",
    "2-4": "法律问题主题识别",
    "2-5": "司法阅读理解",
    "2-6": "法律命名实体识别",
    "2-7": "司法舆情摘要",
    "2-8": "法律论点挖掘",
    "2-9": "法律事件检测",
    "2-10": "法律触发词提取",
    "3-1": "事实法条预测",
    "3-2": "场景法律依据",
    "3-3": "罪名预测",
    "3-4": "刑期预测",
    "3-5": "给定法条刑期预测",
    "3-6": "司法考试案例分析",
    "3-7": "犯罪金额计算",
    "3-8": "法律咨询",
}


@lru_cache(maxsize=20)
def load_task(task_id: str) -> tuple[dict[str, str], ...]:
    if task_id not in TASK_NAMES:
        raise ValueError(f"未知 LawBench 任务：{task_id}")
    path = LAW_BENCH_DIR / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"LawBench 数据缺失：{path}")
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"LawBench 数据格式错误：{path}")
    return tuple(records)


def validate_lawbench() -> dict[str, Any]:
    task_counts = {task_id: len(load_task(task_id)) for task_id in TASK_NAMES}
    invalid = {task_id: count for task_id, count in task_counts.items() if count != 500}
    return {
        "valid": not invalid,
        "total_questions": sum(task_counts.values()),
        "total_tasks": len(task_counts),
        "task_counts": task_counts,
        "invalid_tasks": invalid,
    }


def sample_questions(
    limit: int = 20,
    task_id: str | None = None,
    seed: int = 42,
    include_answers: bool = False,
) -> list[dict[str, Any]]:
    if limit < 0 or limit > 200:
        raise ValueError("limit 必须在 0 到 200 之间")
    task_ids = [task_id] if task_id else list(TASK_NAMES)
    if any(value not in TASK_NAMES for value in task_ids):
        raise ValueError(f"未知 LawBench 任务：{task_id}")
    pool = [
        {"task_id": current, "task_name": TASK_NAMES[current], "index": index, **record}
        for current in task_ids
        for index, record in enumerate(load_task(current))
    ]
    rng = random.Random(seed)
    chosen = rng.sample(pool, min(limit, len(pool)))
    if not include_answers:
        chosen = [{key: value for key, value in item.items() if key != "answer"} for item in chosen]
    return chosen


def lawbench_summary(limit: int = 6, task_id: str | None = None, seed: int = 42) -> dict[str, Any]:
    validation = validate_lawbench()
    return {
        **validation,
        "dataset": "LawBench zero-shot",
        "source": LAW_BENCH_SOURCE,
        "source_commit": LAW_BENCH_COMMIT,
        "license_note": "LawBench 是混合来源数据集，使用时还应遵循各原始任务的数据许可。",
        "tasks": [
            {"id": key, "name": value, "questions": validation["task_counts"][key]}
            for key, value in TASK_NAMES.items()
        ],
        "examples": sample_questions(limit, task_id, seed, include_answers=False),
    }


def extract_choice(text: str) -> str | None:
    patterns = (
        r"\[正确答案\]\s*[:：]?\s*([A-D])",
        r"正确答案\s*[:：]?\s*([A-D])",
        r"(?:答案|选择)\s*[:：]?\s*([A-D])",
    )
    for pattern in patterns:
        match = re.search(pattern, text.upper())
        if match:
            return match.group(1)
    stripped = text.strip().upper()
    return stripped if stripped in {"A", "B", "C", "D"} else None
