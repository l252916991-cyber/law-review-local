"""Run additional frozen local strategies without mutating an existing campaign."""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_metrics import SCORER_VERSION, score_lawbench_item
from app.benchmark_reporting import atomic_json
from app.benchmark_retrieval import corpus_fingerprint
from app.benchmark_solver import _configuration
from app.lawbench import TASK_NAMES
from scripts.benchmark_campaign import digest, probe_model, read_rows, report, service_may_still_be_busy, validate_campaign, write_rows

SOURCES = ["scripts/benchmark_experiment.py", "scripts/benchmark_campaign.py", "app/benchmark_solver.py",
           "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py", "app/benchmark_postprocess.py",
           "app/benchmark_event_tools.py", "app/benchmark_summary_tools.py", "app/legal_corpus.py",
           "app/services.py", "app/config.py"]


def configuration(profile_file: Path) -> dict[str, Any]:
    config = json.loads(profile_file.read_text())
    allowed = {"url", "model", "temperature", "max_tokens", "timeout", "enable_thinking", "strategy", "corpus_directories"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("Unexpected experiment configuration fields")
    rag = config.get("strategy") == "statutory_rag"
    effective = _configuration({**config, "strategy": "task_guided"} if rag else config)
    if rag:
        paths = config.get("corpus_directories")
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
            raise ValueError("RAG requires corpus_directories")
        effective.update(strategy="statutory_rag", corpus_directories=[str(Path(path).resolve()) for path in paths])
    elif "corpus_directories" in config:
        raise ValueError("Only RAG may use corpus_directories")
    return effective


def run(directory: Path, name: str, profile_file: Path, split: str = "development",
        per_task: int = 2, resume: bool = False,
        task_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", name):
        raise ValueError("Invalid run name")
    validate_campaign(directory)
    config = configuration(profile_file)
    max_count = {"development": 20, "anchor": 50, "confirmation": 10}.get(split, 0)
    if not 1 <= per_task <= max_count:
        raise ValueError("Invalid split or per_task")
    selected_tasks = tuple(TASK_NAMES) if task_ids is None else task_ids
    if not selected_tasks or len(set(selected_tasks)) != len(selected_tasks) or any(task not in TASK_NAMES for task in selected_tasks):
        raise ValueError("Invalid or duplicate task filter")
    selected = [item for item in read_rows(directory / "inputs.jsonl")
                if item["split"] == split and item["task"] in selected_tasks and item["position_in_task"] < per_task]
    selected.sort(key=lambda item: (item["position_in_task"], list(TASK_NAMES).index(item["task"])))
    references = {item["question_id"]: item["reference"] for item in read_rows(directory / "references.jsonl")}
    manifest: dict[str, Any] = {"experiment_version": 8, "campaign_sha256": digest(directory / "campaign.json"),
                "profile": profile_file.stem, "profile_sha256": digest(profile_file), "config": config, "split": split,
                "selected_tasks": list(selected_tasks),
                "question_ids": [item["question_id"] for item in selected],
                "input_hashes": {item["question_id"]: item["input_hash"] for item in selected},
                "scorer_version": SCORER_VERSION, "sources": {name: digest(ROOT / name) for name in SOURCES},
                "corpus_files": corpus_fingerprint(config.get("corpus_directories", []))}
    if config["strategy"] == "statutory_rag":
        from app.benchmark_rag_solver import solve
    else:
        from app.benchmark_solver import solve
    output = directory / "runs" / name
    with (directory / "inference.lock").open("a") as model_lock:
        try:
            fcntl.flock(model_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another campaign inference process is running") from exc
        probe_model(config)
        if resume:
            if not output.exists() or json.loads((output / "manifest.json").read_text()) != manifest:
                raise ValueError("Resume requires exact original configuration, sources and corpus")
        else:
            output.mkdir(parents=True, exist_ok=False)
            atomic_json(output / "manifest.json", manifest)
            (output / "profile.json").write_bytes(profile_file.read_bytes())
            for source in SOURCES:
                target = output / "source_snapshot" / source
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / source).read_bytes())
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
        status, connection_failures = "interrupted", 0
        try:
            for item in selected:
                if item["question_id"] in completed:
                    continue
                if any(digest(Path(path)) != expected for path, expected in manifest["corpus_files"].items()):
                    raise ValueError("Corpus changed during experiment")
                result = solve(item["task"], item["instruction"], item["question"], dict(config))
                prediction, error = result["prediction"], result.get("error")
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
                rows.append(row)
                write_rows(output / "detailed_results.jsonl", rows)
                report(output, manifest, rows, "running")
                print(f"[{len(rows)}/{len(selected)}] {item['question_id']} score={row['score']:.4f} latency={row['latency_ms']/1000:.2f}s error={error}", flush=True)
                if service_may_still_be_busy(error):
                    raise RuntimeError("Local generation may still occupy the server; stop now and resume only after it is idle")
                connection_failures = connection_failures + 1 if error and "Connection refused" in error else 0
                if connection_failures >= 3:
                    raise RuntimeError("Local server unavailable; three failures retained")
            status = "completed"
        finally:
            summary = report(output, manifest, rows, status)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--profile-file", type=Path, required=True)
    parser.add_argument("--split", choices=["development", "anchor", "confirmation"], default="development")
    parser.add_argument("--per-task", type=int, default=2)
    parser.add_argument("--task", action="append", choices=list(TASK_NAMES), dest="task_ids",
                        help="Run only this frozen task; repeat to select multiple tasks")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    result = run(args.directory, args.name, args.profile_file, args.split, args.per_task, args.resume,
                 tuple(args.task_ids) if args.task_ids else None)
    print(json.dumps({key: value for key, value in result.items() if key != "tasks"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
