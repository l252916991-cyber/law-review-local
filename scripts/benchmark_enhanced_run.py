"""Run the enhanced LawBench pipeline: statutory retrieval plus reference-free post-processing.

The audited `unified_benchmark_runner.py` only supports direct/task_guided prompts and the
campaign runner calls `app.benchmark_solver.solve`, so neither can exercise
`app.benchmark_rag_solver.solve` (task-guided + statutory context + deterministic output
repairs for 1-1/2-1/2-7/2-9/2-10). This runner fills that gap with the same discipline as the
audited runner: frozen manifest with source and corpus hashes, per-question checkpoints,
resumable runs, all-question denominator, and no reference answer in the solver input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import unified_benchmark_runner as runner  # noqa: E402
from app.benchmark_metrics import SCORER_VERSION, score_lawbench_item  # noqa: E402
from app.benchmark_rag_solver import solve  # noqa: E402
from app.benchmark_reporting import PROMPT_VERSION, atomic_json, metrics, write_report  # noqa: E402
from app.benchmark_retrieval import corpus_fingerprint  # noqa: E402

PIPELINE_VERSION = "lawbench-enhanced-v1"
SOURCES = (
    "scripts/benchmark_enhanced_run.py", "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py",
    "app/benchmark_postprocess.py", "app/benchmark_solver.py", "app/benchmark_metrics.py",
    "app/lawbench.py", "app/benchmark_reporting.py",
)
REQUIRED_CONFIG = ("url", "model", "temperature", "max_tokens", "timeout", "enable_thinking")


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def model_prediction(result: dict[str, Any]) -> str:
    """The final model answer before deterministic post-processing replaced it."""
    calls = result.get("calls") or []
    return str(calls[-1].get("prediction", "")) if calls else ""


def run(
    *,
    tasks: list[str],
    limit_per_task: int | None,
    sample_seed: int | None,
    config: dict[str, Any],
    corpus_directories: list[str],
    output_dir: Path,
    run_dir: Path | None,
    resume: bool,
    baseline: Path | None,
) -> dict[str, Any]:
    """Execute or resume a frozen enhanced run; checkpoints are the source of truth."""
    records = runner.load_lawbench_dataset(tasks, limit_per_task, sample_seed)
    if not records:
        raise ValueError("No questions loaded")
    missing = [name for name in REQUIRED_CONFIG if name not in config]
    if missing:
        raise ValueError(f"Missing model configuration: {', '.join(missing)}")
    fingerprint = corpus_fingerprint(corpus_directories)
    if run_dir is not None:
        output_dir = Path(run_dir)
    elif resume:
        raise ValueError("--resume requires the original --run-dir")
    else:
        output_dir = Path(output_dir) / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    manifest_path = output_dir / "manifest.json"
    baseline = baseline.resolve() if baseline else None
    if baseline:
        old = {row["question_id"]: row for line in baseline.read_text(encoding="utf-8").splitlines()
               if line.strip() for row in [json.loads(line)]}
        for record in records:
            previous = old.get(record["question_id"])
            if not previous or (previous["question"], previous["reference"]) != (record["question"], record["reference"]):
                raise ValueError(f"Baseline missing/mismatched: {record['question_id']}")
    manifest: dict[str, Any] = {
        "pipeline_version": PIPELINE_VERSION, "scorer_version": SCORER_VERSION, "prompt_version": PROMPT_VERSION,
        "model_config": dict(config), "strategy": config.get("strategy"),
        "corpus_directories": corpus_directories, "corpus_fingerprint": fingerprint,
        "sample_seed": sample_seed, "planned_questions": len(records),
        "baseline_results": str(baseline) if baseline else None,
        "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest() if baseline else None,
        "source_hashes": source_hashes(),
        "samples": [{"question_id": record["question_id"], "question_hash": record["question_hash"],
                     "record_hash": hash_text(json.dumps(record, ensure_ascii=False, sort_keys=True))}
                    for record in records],
    }
    if resume:
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("Resume manifest differs (model, corpus, sample, source or scorer); start a new run")
    else:
        output_dir.mkdir(parents=True, exist_ok=False)
        atomic_json(manifest_path, manifest)
        for name in manifest["source_hashes"]:
            destination = output_dir / "source_snapshot" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT / name).read_bytes())

    checkpoints = output_dir / "checkpoints"
    checkpoints.mkdir(exist_ok=True)
    solver_config = {**config, "corpus_directories": corpus_directories}
    planned = {record["question_id"]: record for record in records}
    completed: dict[str, dict[str, Any]] = {}
    for path in sorted(checkpoints.glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        key = row["question_id"]
        if key not in planned or key in completed or row.get("record_hash") != hash_text(
                json.dumps(planned[key], ensure_ascii=False, sort_keys=True)):
            raise ValueError(f"Unexpected or changed checkpoint: {key}")
        completed[key] = row
    rows = [completed[record["question_id"]] for record in records if record["question_id"] in completed]
    detail_path = output_dir / "detailed_results.jsonl"
    detail_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    started = time.perf_counter()
    print(f"Run: {output_dir.resolve()} | {len(rows)}/{len(records)} already saved", flush=True)
    runner.verify_model(config)
    write_report(rows, output_dir, 0.0, config, manifest, "running")
    try:
        for record in records:
            key = record["question_id"]
            if key in completed:
                continue
            result = solve(record["task"], record["instruction"], record["question"], dict(solver_config))
            prediction, error = str(result.get("prediction", "")), result.get("error")
            if error and "truncated_response:" in error and result.get("finish_reason") == "length":
                result, error = {**result, "output_warning": error, "error": None}, None
            scoring = score_lawbench_item(
                record["task"], prediction, record["reference"], question=record["question"],
            ).to_dict() if not error else {
                "score": 0.0, "metric": "error", "abstained": not prediction, "parse_failed": False,
                "reference_invalid": False, "parsed_prediction": None, "parsed_reference": None,
            }
            row = {
                **record, **scoring, "prediction": prediction, "model_prediction": model_prediction(result),
                "error": error, "latency_ms": result.get("latency_ms", 0.0), "calls": result.get("calls", []),
                "retrieval": result.get("retrieval"), "postprocess": result.get("postprocess"),
                "finish_reason": result.get("finish_reason"), "usage": result.get("usage"),
                "model_config": solver_config, "solver_version": result.get("solver_version"),
                "scorer_version": SCORER_VERSION, "pipeline_version": PIPELINE_VERSION,
                "record_hash": hash_text(json.dumps(record, ensure_ascii=False, sort_keys=True)),
                "timestamp": datetime.now().isoformat(),
            }
            atomic_json(checkpoints / f"{key}.json", row)
            with detail_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
            completed[key] = row
            rows.append(row)
            print(f"[{len(rows)}/{len(records)}] {key} score={row['score']:.4f} "
                  f"post={bool((row['postprocess'] or {}).get('applied'))} error={error}", flush=True)
            if len(rows) % 100 == 0:
                write_report(rows, output_dir, time.perf_counter() - started, config, manifest, "running")
        status = "completed_with_errors" if any(row.get("error") for row in rows) else "completed"
    except BaseException:
        write_report(rows, output_dir, time.perf_counter() - started, config, manifest, "interrupted")
        raise
    summary = write_report(rows, output_dir, time.perf_counter() - started, config, manifest, status)
    print(f"Finished: {output_dir.resolve()} ({status}) mean={summary['mean_score_all']:.4f}", flush=True)
    return summary


def verify_run(directory: Path, *, write: bool = True) -> dict[str, Any]:
    """Offline integrity check: completeness, checkpoints, score recomputation, sources."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (directory / "detailed_results.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    issues: list[str] = []
    planned = {sample["question_id"]: sample for sample in manifest["samples"]}
    if summary["status"] not in {"completed", "completed_with_errors"}:
        issues.append("Run is not complete")
    if len(rows) != manifest["planned_questions"] or {row["question_id"] for row in rows} != set(planned):
        issues.append("Missing, duplicate or unexpected results")
    if source_hashes() != manifest["source_hashes"]:
        issues.append("Live pipeline source differs from frozen manifest")
    if corpus_fingerprint(manifest["corpus_directories"]) != manifest["corpus_fingerprint"]:
        issues.append("Corpus content changed since the run")
    for row in rows:
        key = row["question_id"]
        checkpoint = directory / "checkpoints" / f"{key}.json"
        if not checkpoint.exists() or json.loads(checkpoint.read_text(encoding="utf-8")) != row:
            issues.append(f"Checkpoint mismatch: {key}")
        if hash_text(json.dumps({name: row[name] for name in (
                "dataset", "task", "task_name", "question_id", "question", "instruction",
                "dataset_version", "reference", "question_hash")}, ensure_ascii=False, sort_keys=True)) != planned[key]["record_hash"]:
            issues.append(f"Record mismatch: {key}")
        if not row.get("error"):
            rescored = score_lawbench_item(row["task"], row["prediction"], row["reference"],
                                           question=row["question"]).to_dict()
            if any(row.get(name) != value for name, value in rescored.items()):
                issues.append(f"Score mismatch: {key}")
    recalculated = metrics(rows)
    for name, value in recalculated.items():
        if summary.get(name) != value:
            issues.append(f"Summary mismatch: {name}")
    result = {"valid": not issues, "issues": issues, "questions": len(rows),
              "scoring_verified": not issues, "mean_score_all": summary.get("mean_score_all")}
    if write:
        atomic_json(directory / "verification.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="+", default=["all"])
    parser.add_argument("--limit-per-task", type=int, default=None)
    parser.add_argument("--sample-seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=Path("output/enhanced_runs"))
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--baseline-results", type=Path)
    parser.add_argument("--corpus-dir", type=Path, action="append", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default="Qwythos-9B-v2-8bit-mlx")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=1600)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--verify", type=Path, help="只校验已有运行目录，不调用模型")
    args = parser.parse_args()
    if args.verify:
        result = verify_run(args.verify)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 1
    config = {
        "url": args.url, "model": args.model, "temperature": args.temperature,
        "max_tokens": args.max_tokens, "timeout": args.timeout, "enable_thinking": False,
        "strategy": "statutory_rag",
    }
    summary = run(
        tasks=args.tasks, limit_per_task=args.limit_per_task, sample_seed=args.sample_seed,
        config=config, corpus_directories=[str(path) for path in args.corpus_dir],
        output_dir=args.output_dir, run_dir=args.run_dir, resume=args.resume,
        baseline=args.baseline_results,
    )
    print(json.dumps({key: summary[key] for key in (
        "status", "total_questions", "mean_score_all", "failed", "parse_failed", "truncated")},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
