"""Frozen score-85 experiments: split preparation, blind solving and resumable runs."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import random
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_metrics import SCORER_VERSION, score_lawbench_item
from app.benchmark_reporting import atomic_json, metrics
from app import lawbench
from app.lawbench import LAW_BENCH_COMMIT, TASK_NAMES, load_task


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def value_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)


def profiles() -> dict[str, dict[str, Any]]:
    base = {"url": "http://127.0.0.1:8000/v1", "temperature": 0.0, "max_tokens": 1600,
            "timeout": 240, "enable_thinking": False, "strategy": "task_guided"}
    candidates = {
        "qwythos": "Qwythos-9B-v2-4bit-mlx",
        "qwen35": "Qwen3.5-9B-4bit",
        "qwen36": "Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-OptiQ-5bpw-MLX",
    }
    result = {"qwythos-direct": {**base, "model": candidates["qwythos"], "max_tokens": 900, "strategy": "direct"}}
    for name, model in candidates.items():
        result[f"{name}-guided"] = {**base, "model": model}
        result[f"{name}-thinking"] = {**base, "model": model, "enable_thinking": True, "max_tokens": 4096, "temperature": 0.6}
    return result


def probe_model(config: dict[str, Any]) -> None:
    from app.benchmark_solver import _endpoint, _NoRedirect

    endpoint = _endpoint(config["url"]).removesuffix("/chat/completions") + "/models"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(endpoint, timeout=10) as response:
        payload = json.load(response)
    available = {item["id"] for item in payload.get("data", [])}
    if config["model"] not in available:
        raise ValueError(f"Configured model is unavailable: {config['model']}")


def prepare(baseline: Path, destination: Path, seed: int = 8505) -> dict[str, Any]:
    if destination.exists():
        raise FileExistsError("Campaign already exists; frozen splits cannot be overwritten")
    baseline = baseline.resolve()
    rows = read_rows(baseline / "detailed_results.jsonl")
    if len(rows) != 1000 or Counter(row["task"] for row in rows) != {task: 50 for task in TASK_NAMES}:
        raise ValueError("Anchor baseline must contain exactly 50 questions per task")
    anchor = {row["question_id"]: row for row in rows}
    if len(anchor) != 1000:
        raise ValueError("Duplicate anchor IDs")
    inputs: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    for task in TASK_NAMES:
        data = load_task(task)
        expected_ids = {f"{task}_{index:04d}" for index in range(len(data))}
        task_anchor = {key for key, row in anchor.items() if row["task"] == task}
        if not task_anchor.issubset(expected_ids):
            raise ValueError("Unknown anchor IDs")
        remaining = sorted(expected_ids - task_anchor)
        random.Random(f"{seed}:{task}").shuffle(remaining)
        development, confirmation = remaining[:20], remaining[20:30]
        splits = [("anchor", sorted(task_anchor)), ("development", development), ("confirmation", confirmation)]
        for split, identifiers in splits:
            for position, key in enumerate(identifiers):
                original = data[int(key.rsplit("_", 1)[1])]
                if split == "anchor" and (anchor[key]["question"] != original["question"] or anchor[key]["reference"] != original["answer"] or anchor[key]["instruction"] != original["instruction"]):
                    raise ValueError(f"Anchor content mismatch: {key}")
                item = {"question_id": key, "task": task, "task_name": TASK_NAMES[task], "split": split,
                        "position_in_task": position, "instruction": original["instruction"], "question": original["question"]}
                inputs.append({**item, "input_hash": value_hash(item)})
                references.append({"question_id": key, "reference": original["answer"]})
    if len({row["question_id"] for row in inputs}) != len(inputs):
        raise ValueError("Overlapping splits")
    destination.mkdir(parents=True)
    write_rows(destination / "inputs.jsonl", inputs)
    write_rows(destination / "references.jsonl", references)
    frozen_files = {"app/benchmark_metrics.py": digest(ROOT / "app/benchmark_metrics.py"),
                    "app/lawbench.py": digest(ROOT / "app/lawbench.py")}
    manifest = {
        "campaign_version": 1, "goal_score": 85.0, "scorer_version": SCORER_VERSION,
        "dataset_commit": LAW_BENCH_COMMIT, "seed": seed,
        "baseline": str(baseline), "baseline_sha256": digest(baseline / "detailed_results.jsonl"),
        "baseline_mean_score": sum(row["score"] for row in rows) / len(rows),
        "split_counts": dict(Counter(row["split"] for row in inputs)),
        "files": {name: digest(destination / name) for name in ("inputs.jsonl", "references.jsonl")},
        "frozen_sources": frozen_files,
        "dataset_files": {task: digest(lawbench.LAW_BENCH_DIR / f"{task}.json") for task in TASK_NAMES},
        "profiles": profiles(),
        "note": "Development only for selection; anchor previously inspected. Confirmation held out. No gold in solve inputs.",
    }
    atomic_json(destination / "campaign.json", manifest)
    for name in frozen_files:
        target = destination / "frozen_sources" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    return manifest


def validate_campaign(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "campaign.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if name not in {"inputs.jsonl", "references.jsonl"} or digest(directory / name) != expected:
            raise ValueError(f"Changed campaign file: {name}")
    for name, expected in manifest["frozen_sources"].items():
        if name not in {"app/benchmark_metrics.py", "app/lawbench.py"} or digest(ROOT / name) != expected:
            raise ValueError(f"Frozen scorer changed: {name}")
    for task, expected in manifest["dataset_files"].items():
        if task not in TASK_NAMES or digest(lawbench.LAW_BENCH_DIR / f"{task}.json") != expected:
            raise ValueError(f"Frozen dataset changed: {task}")
    if SCORER_VERSION != manifest["scorer_version"]:
        raise ValueError("Scorer version changed")
    return manifest


def report(directory: Path, manifest: dict[str, Any], rows: list[dict[str, Any]], status: str) -> dict[str, Any]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_task[row["task"]].append(row)
    totals = metrics(rows)
    summary = {
        "status": status, "profile": manifest["profile"], "split": manifest["split"],
        "planned": len(manifest["question_ids"]), **totals,
        "score_100": totals["mean_score_all"] * 100,
        "model_calls": sum(len(row.get("calls", [])) for row in rows),
        "tasks": {task: metrics(items) for task, items in by_task.items()},
        "anchor_threshold_reached": (status == "completed" and manifest["split"] == "anchor"
                                     and len(rows) == 1000 and totals["mean_score_all"] >= .85),
        "goal_complete": False,
        "note": "Anchor >=85 still requires independent confirmation and app integration. Development scores are not acceptance.",
    }
    atomic_json(directory / "summary.json", summary)
    lines = [f"# Score-85 实验：{manifest['profile']}", "",
             f"数据：{manifest['split']}，{len(rows)}/{len(manifest['question_ids'])} 题；状态：{status}。",
             f"全题混合均分：{summary['score_100']:.4f} / 100；技术失败 {totals['failed']}，空答 {totals['empty_responses']}，截断 {totals['truncated']}。",
             f"实际模型调用：{summary['model_calls']}；平均题耗时 {(totals['avg_latency_ms'] or 0)/1000:.2f} 秒。",
             "开发集与部分结果不代表达到 85 分目标。", "",
             "| 任务 | 题数 | 均分 | 错误 |", "|---|---:|---:|---:|"]
    lines += [f"| {task} {TASK_NAMES[task]} | {m['total']} | {m['mean_score_all']*100:.2f} | {m['failed']} |" for task, m in summary["tasks"].items()]
    (directory / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def service_may_still_be_busy(error: str | None) -> bool:
    """A timed-out local generation can keep occupying the single-slot server."""
    return bool(error and ("TimeoutError" in error or "HTTP Error 409" in error
                           or "HTTP Error 507" in error or "IncompleteRead" in error))


def run(directory: Path, name: str, profile: str, split: str, per_task: int, resume: bool = False) -> dict[str, Any]:
    from app.benchmark_solver import solve

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", name):
        raise ValueError("Invalid run name")
    campaign = validate_campaign(directory)
    if profile not in campaign["profiles"] or split not in {"development", "anchor", "confirmation"}:
        raise ValueError("Unknown profile or split")
    max_count = {"development": 20, "anchor": 50, "confirmation": 10}[split]
    if not 1 <= per_task <= max_count:
        raise ValueError("per_task outside frozen split size")
    selected = [item for item in read_rows(directory / "inputs.jsonl") if item["split"] == split and item["position_in_task"] < per_task]
    # Interleave tasks so early interruption does not exclusively cover one task.
    selected.sort(key=lambda item: (item["position_in_task"], list(TASK_NAMES).index(item["task"])))
    references = {item["question_id"]: item["reference"] for item in read_rows(directory / "references.jsonl")}
    sources = ["scripts/benchmark_campaign.py", "app/benchmark_solver.py", "app/services.py", "app/config.py"]
    manifest = {"campaign_sha256": digest(directory / "campaign.json"), "profile": profile, "split": split,
                "config": campaign["profiles"][profile], "question_ids": [item["question_id"] for item in selected],
                "input_hashes": {item["question_id"]: item["input_hash"] for item in selected},
                "scorer_version": SCORER_VERSION,
                "sources": {name: digest(ROOT / name) for name in sources}}
    output = directory / "runs" / name
    with (directory / "inference.lock").open("a") as model_lock:
        try:
            fcntl.flock(model_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another campaign inference process is running") from exc
        probe_model(manifest["config"])
        if resume:
            if not output.exists() or json.loads((output / "manifest.json").read_text()) != manifest:
                raise ValueError("Resume requires the original exact manifest, sources and configuration")
        else:
            output.mkdir(parents=True, exist_ok=False)
            atomic_json(output / "manifest.json", manifest)
            for name in sources:
                target = output / "source_snapshot" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
        checkpoints = output / "checkpoints"
        checkpoints.mkdir(exist_ok=True)
        completed: dict[str, dict[str, Any]] = {}
        for path in checkpoints.glob("*.json"):
            row = json.loads(path.read_text())
            key = row["question_id"]
            if key not in manifest["input_hashes"] or row["input_hash"] != manifest["input_hashes"][key] or key in completed:
                raise ValueError("Unexpected or changed checkpoint")
            completed[key] = row
        rows = [completed[item["question_id"]] for item in selected if item["question_id"] in completed]
        write_rows(output / "detailed_results.jsonl", rows)
        report(output, manifest, rows, "running")
        status = "interrupted"
        connection_failures = 0
        try:
            for item in selected:
                if item["question_id"] in completed:
                    continue
                # Explicit blind interface: no reference, prior score or baseline answer.
                result = solve(item["task"], item["instruction"], item["question"], dict(manifest["config"]))
                prediction, error = result["prediction"], result.get("error")
                # Preserve v3's original treatment of nonempty truncated text:
                # score the saved final fragment and report truncation separately.
                # A solver output-contract warning is not a transport failure.
                if error and "truncated_response:" in error and result.get("finish_reason") == "length":
                    result = {**result, "output_warning": error, "error": None}
                    error = None
                scoring = score_lawbench_item(item["task"], prediction, references[item["question_id"]], question=item["question"]).to_dict() if not error else {
                    "score": 0.0, "metric": "error", "abstained": not prediction, "parse_failed": False,
                    "reference_invalid": False, "parsed_prediction": None, "parsed_reference": None,
                }
                row = {**item, **result, **scoring, "dataset": "lawbench", "reference": references[item["question_id"]],
                       "scorer_version": SCORER_VERSION, "completed_at": time.time()}
                atomic_json(checkpoints / f"{item['question_id']}.json", row)
                completed[item["question_id"]] = row
                rows.append(row)
                write_rows(output / "detailed_results.jsonl", rows)
                report(output, manifest, rows, "running")
                print(f"[{len(rows)}/{len(selected)}] {item['question_id']} score={row['score']:.4f} latency={row['latency_ms']/1000:.2f}s error={error}", flush=True)
                if service_may_still_be_busy(error):
                    raise RuntimeError("Local generation may still occupy the server; stop now and resume only after it is idle")
                connection_failures = connection_failures + 1 if error and "Connection refused" in error else 0
                if connection_failures >= 3:
                    raise RuntimeError("Local server stopped; three failed attempts retained, remaining items not attempted")
            status = "completed"
        finally:
            summary = report(output, manifest, rows, status)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    setup.add_argument("--baseline", type=Path, required=True)
    setup.add_argument("--directory", type=Path, required=True)
    setup.add_argument("--seed", type=int, default=8505)
    execute = commands.add_parser("run")
    execute.add_argument("--directory", type=Path, required=True)
    execute.add_argument("--name", required=True)
    execute.add_argument("--profile", required=True)
    execute.add_argument("--split", default="development", choices=["development", "anchor", "confirmation"])
    execute.add_argument("--per-task", type=int, default=2)
    execute.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    result = prepare(args.baseline, args.directory, args.seed) if args.command == "prepare" else run(args.directory, args.name, args.profile, args.split, args.per_task, args.resume)
    print(json.dumps({key: value for key, value in result.items() if key not in {"dataset_files", "profiles", "tasks"}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
