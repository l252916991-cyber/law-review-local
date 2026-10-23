"""Measure statutory-corpus coverage for saved LawBench answers; never calls a model.

Task 1-1 asks for the text of a named article, so coverage is the share of saved
questions that resolve to an exact official article in the frozen corpus. The
uncovered questions are aggregated by law name and can be written as a
campaign-compatible ``inputs.jsonl`` worklist that ``scripts/build_npc_corpus.py``
consumes to fetch exactly the missing publications.

Retrieval selection uses questions only; reference answers are never read.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_retrieval import SUPPORTED_TASKS, retrieve  # noqa: E402
from scripts.build_npc_corpus import QUESTION_LAW, canonical_law_name  # noqa: E402

WORKLIST_FIELDS = ("question_id", "task", "task_name", "instruction", "question")


def load_rows(run: Path, tasks: list[str]) -> list[dict[str, Any]]:
    """Load saved rows for the requested tasks from an immutable run directory."""
    unknown = sorted(set(tasks) - SUPPORTED_TASKS)
    if unknown:
        raise ValueError(f"Retrieval does not support task(s): {', '.join(unknown)}")
    rows = [
        json.loads(line)
        for line in (run / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = [row for row in rows if row["task"] in set(tasks)]
    if not selected:
        raise ValueError(f"No saved rows for task(s): {', '.join(tasks)}")
    return selected


def coverage(rows: list[dict[str, Any]], directories: list[str], tasks: list[str]) -> dict[str, Any]:
    """Report per-task retrieval modes and the uncovered 1-1 laws."""
    result: dict[str, Any] = {"corpus_directories": directories, "tasks": {}}
    missing: dict[str, dict[str, Any]] = defaultdict(lambda: {"short_names": set(), "questions": 0, "question_ids": []})
    worklist: list[dict[str, Any]] = []
    for task in tasks:
        task_rows = [row for row in rows if row["task"] == task]
        modes: Counter[str] = Counter()
        covered = 0
        hits = 0
        for row in task_rows:
            retrieval = retrieve(task, row["question"], directories)
            modes[retrieval["mode"]] += 1
            if retrieval["hits"]:
                hits += 1
            if task == "1-1" and retrieval["mode"] == "exact_article" and retrieval["hits"]:
                covered += 1
                continue
            if task == "1-1":
                match = QUESTION_LAW.search(row["question"])
                if not match:
                    raise ValueError(f"Cannot extract law name from saved question: {row['question_id']}")
                name = canonical_law_name(match.group(1))
                entry = missing[name]
                entry["short_names"].add(match.group(1))
                entry["questions"] += 1
                entry["question_ids"].append(row["question_id"])
                worklist.append({field: row[field] for field in WORKLIST_FIELDS})
        result["tasks"][task] = {
            "total": len(task_rows), "modes": dict(modes), "with_hits": hits,
            "exact_article_covered": covered if task == "1-1" else None,
            "coverage": covered / len(task_rows) if task == "1-1" and task_rows else None,
        }
    result["missing_laws"] = [
        {
            "law_name": name,
            "short_names": sorted(entry["short_names"]),
            "questions": entry["questions"],
            "question_ids": entry["question_ids"],
        }
        for name, entry in sorted(missing.items(), key=lambda item: (-item[1]["questions"], item[0]))
    ]
    result["missing_law_count"] = len(result["missing_laws"])
    result["missing_question_count"] = len(worklist)
    result["worklist"] = worklist
    return result


def write_worklist(worklist: list[dict[str, Any]], path: Path) -> None:
    """Write campaign-compatible inputs.jsonl for build_npc_corpus.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in worklist), encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="含 detailed_results.jsonl 的运行目录")
    parser.add_argument("--corpus-dir", type=Path, action="append", required=True, help="可重复：法条库目录")
    parser.add_argument("--task", action="append", default=None, help="可重复：默认 1-1")
    parser.add_argument("--worklist", type=Path, help="把未覆盖的 1-1 题写成 build_npc_corpus 兼容的 inputs.jsonl")
    parser.add_argument("--output", type=Path, help="把完整结果写成 JSON")
    args = parser.parse_args()
    tasks = args.task or ["1-1"]
    directories = [str(directory) for directory in args.corpus_dir]
    result = coverage(load_rows(args.run, tasks), directories, tasks)
    if args.worklist:
        write_worklist(result["worklist"], args.worklist)
        result["worklist_path"] = str(args.worklist)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "worklist"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
