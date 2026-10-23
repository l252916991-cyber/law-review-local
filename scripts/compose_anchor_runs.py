"""Compose a frozen 1,000-question anchor score from per-task frozen runs.

Each anchor question must appear exactly once across the source runs. Rows are
re-scored with the frozen local scorer; no model is invoked and no answer is
edited. The composition is anchor-validated routing, not a blind experiment.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.benchmark_metrics import SCORER_VERSION, score_lawbench_item
from app.lawbench import TASK_NAMES


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def compose(runs: list[tuple[Path, set[str] | None]], baseline: Path, destination: Path) -> dict:
    baseline_rows = read_rows(baseline)
    baseline_by_id = {row["question_id"]: row for row in baseline_rows}
    if len(baseline_rows) != 1000 or len(baseline_by_id) != 1000:
        raise ValueError("Baseline must contain exactly 1,000 unique anchor questions")
    composed: list[dict] = []
    origin: dict[str, str] = {}
    for source, task_filter in runs:
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
        if summary.get("status") != "completed" or manifest.get("scorer_version") != SCORER_VERSION:
            raise ValueError(f"Run not completed or scorer mismatch: {source.name}")
        rows = read_rows(source / "detailed_results.jsonl")
        if task_filter is not None:
            rows = [row for row in rows if row["task"] in task_filter]
        for row in rows:
            key = row["question_id"]
            if row.get("error"):
                raise ValueError(f"Composed runs must not contain failed questions: {key} in {source.name}")
            if key in origin:
                raise ValueError(f"Question covered twice: {key} ({origin[key]} and {source.name})")
            if key not in baseline_by_id or baseline_by_id[key]["question"] != row["question"]:
                raise ValueError(f"Question mismatch against baseline: {key}")
            origin[key] = source.name
            composed.append({**row, "composed_from": source.name})
    if len(composed) != 1000:
        raise ValueError(f"Composition covers {len(composed)} of 1,000 questions")
    if Counter(row["task"] for row in composed) != Counter({task: 50 for task in TASK_NAMES}):
        raise ValueError("Composition does not cover 50 questions per task")
    for row in composed:
        scored = score_lawbench_item(row["task"], row["prediction"], row["reference"], question=row["question"]).to_dict()
        if abs(scored["score"] - row["score"]) > 1e-9:
            raise ValueError(f"Score recompute mismatch: {row['question_id']}")
        row["baseline_score"] = baseline_by_id[row["question_id"]]["score"]
        row["delta"] = row["score"] - row["baseline_score"]
    overall = {
        "questions": len(composed),
        "mean_score_all": statistics.mean(row["score"] for row in composed),
        "baseline_mean_score_all": statistics.mean(row["baseline_score"] for row in composed),
        "delta": statistics.mean(row["delta"] for row in composed),
        "improved": sum(row["delta"] > 1e-9 for row in composed),
        "regressed": sum(row["delta"] < -1e-9 for row in composed),
        "failed": sum(bool(row.get("error")) for row in composed),
        "truncated": sum(row.get("finish_reason") == "length" for row in composed),
        "scorer_version": SCORER_VERSION,
    }
    by_task: dict[str, list[dict]] = defaultdict(list)
    for row in composed:
        by_task[row["task"]].append(row)
    tasks = {task: {"name": TASK_NAMES[task], "total": len(rows),
                    "mean_score_all": statistics.mean(r["score"] for r in rows),
                    "baseline_mean_score_all": statistics.mean(r["baseline_score"] for r in rows),
                    "delta": statistics.mean(r["delta"] for r in rows),
                    "composed_from": sorted({r["composed_from"] for r in rows})}
             for task, rows in by_task.items()}
    destination.mkdir(parents=True, exist_ok=False)
    payload = {"overall": overall, "tasks": tasks,
               "sources": [{"name": s.name, "tasks": sorted(t) if t is not None else None,
                            "manifest": json.loads((s / 'manifest.json').read_text(encoding='utf-8'))} for s, t in runs]}
    (destination / "composed_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (destination / "composed_results.jsonl").open("w", encoding="utf-8") as stream:
        for row in composed:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, type=Path, help="Frozen run directory; repeat for each source")
    parser.add_argument("--only-task", action="append", type=str, default=None,
                        help="Restrict the preceding --run to these comma-separated tasks (e.g. 1-1,3-1)")
    parser.add_argument("--baseline", type=Path, required=True, help="Anchor baseline detailed_results.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs: list[tuple[Path, set[str] | None]] = []
    filters: list[set[str] | None] = [None] * len(args.run)
    if args.only_task:
        for spec in args.only_task:
            index, _, tasks = spec.partition("=")
            filters[int(index)] = {value.strip() for value in tasks.split(",") if value.strip()}
    for path, task_filter in zip(args.run, filters):
        runs.append((path, task_filter))
    payload = compose(runs, args.baseline, args.output)
    print(json.dumps({"overall": payload["overall"],
                      "tasks": {k: {kk: v[kk] for kk in ("mean_score_all", "baseline_mean_score_all", "delta")} for k, v in payload["tasks"].items()}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
