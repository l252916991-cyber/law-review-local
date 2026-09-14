"""Config-consistent full-scale paired evaluation for one LawBench task.

Runs the same N questions through two arms that differ only in retrieval material —
``tg`` (task-guided, no retrieval) and ``lex`` (statutory RAG, lexical retrieval) —
using the exact solver entry points the anchor protocol used. Every question counts
in the denominator; errors score zero. Per-question checkpoints make the run
resumable after infra failure; a resumed run keeps the original name and appends.

This is a full-scale evaluation, not an independent confirmation or blind test:
the questions include previously inspected ones.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_metrics import SCORER_VERSION, score_lawbench_item  # noqa: E402
from app.benchmark_retrieval import corpus_fingerprint  # noqa: E402
from app.lawbench import load_task  # noqa: E402

SOURCES = ("app/benchmark_solver.py", "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py",
           "app/benchmark_metrics.py", "app/legal_corpus.py", "app/benchmark_postprocess.py",
           "app/lawbench.py", "scripts/fullscale_paired.py")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_profile(path: Path) -> dict[str, Any]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("strategy") not in {"task_guided", "statutory_rag"}:
        raise ValueError("profile strategy must be task_guided or statutory_rag")
    return profile


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


TRANSPORT_ERROR = ("URLError", "ConnectionError", "TimeoutError", "OSError", "Connection refused")


def _transport_failure(error: str) -> bool:
    """True when an error row is infrastructure-level, so resume must regenerate it."""
    return any(marker in error for marker in TRANSPORT_ERROR)


def run_arm(arm: str, task_id: str, rows_in: list[dict[str, Any]], expected: dict[str, str],
            profile: dict[str, Any], output: Path, sources: dict[str, str]) -> list[dict[str, Any]]:
    from app.benchmark_rag_solver import solve as rag_solve
    from app.benchmark_solver import solve as base_solve

    arm_dir = output / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = arm_dir / "detailed_results.jsonl"
    done: dict[str, dict[str, Any]] = {}
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if expected.get(row["question_id"]) != row.get("input_hash"):
                    raise ValueError(f"checkpoint input changed for {row['question_id']}")
                if row.get("error") and _transport_failure(row["error"]):
                    # Infra failure, not a model failure: regenerate instead of
                    # keeping a spurious zero in the denominator.
                    continue
                done[row["question_id"]] = row
        print(f"[{arm}] resuming with {len(done)} checkpoints", flush=True)
    config = {**profile, "task": task_id}
    manifest = {"arm": arm, "task": task_id, "config": config, "sources": sources,
                "scorer_version": SCORER_VERSION,
                "corpus_files": corpus_fingerprint(profile.get("corpus_directories", []))
                if profile.get("corpus_directories") else None,
                "expected_question_ids": [row["question_id"] for row in rows_in]}
    (arm_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    transport_failures = 0
    consecutive_failures = 0
    for position, item in enumerate(rows_in):
        prior = done.get(item["question_id"])
        if prior is not None:
            rows.append(prior)
            continue
        if profile["strategy"] == "statutory_rag":
            result = rag_solve(task_id, item["instruction"], item["question"], {**profile, "task": task_id})
        else:
            result = base_solve(task_id, item["instruction"], item["question"], {**profile, "task": task_id})
        prediction, error = result.get("prediction", ""), result.get("error")
        if error and _transport_failure(error):
            # Infrastructure failure: never write a spurious zero. The question is
            # simply absent from the checkpoint, so a repaired rerun fills it in;
            # abort early when the service is clearly down.
            transport_failures += 1
            consecutive_failures += 1
            print(f"[{arm}] transport failure at {item['question_id']}: {error}", flush=True)
            if consecutive_failures >= 3:
                raise RuntimeError(f"{consecutive_failures} transport failures in a row; "
                                   f"aborting {arm} for repair (checkpoints kept)")
            continue
        consecutive_failures = 0
        if error:
            score = {"score": 0.0, "metric": "error"}
        else:
            score = score_lawbench_item(task_id, prediction, item["reference"], question=item["question"]).to_dict()
        row = {"arm": arm, "question_id": item["question_id"], "task": task_id,
               "instruction": item["instruction"], "question": item["question"],
               "reference": item["reference"], "prediction": prediction, "error": error,
               "finish_reason": result.get("finish_reason"), "usage": result.get("usage"),
               "latency_ms": result.get("latency_ms"), "score": score["score"],
               "metric": score.get("metric"), "scorer_version": SCORER_VERSION,
               "solver_version": result.get("solver_version"),
               "input_hash": expected[item["question_id"]],
               "completed_at": time.monotonic()}
        rows.append(row)
        done[item["question_id"]] = row
        write_rows(checkpoint, list(done.values()))
        print(f"[{arm} {position + 1}/{len(rows_in)}] {item['question_id']} "
              f"score={row['score']:.4f} error={error}", flush=True)
    print(f"[{arm}] mean={sum(r['score'] for r in rows) / len(rows):.4f} "
          f"elapsed={(time.monotonic() - started) / 60:.1f}min", flush=True)
    if transport_failures:
        raise RuntimeError(f"{arm} completed with {transport_failures} missing questions "
                           f"(transport failures); repair and rerun to fill them")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--profile-tg", type=Path, required=True)
    parser.add_argument("--profile-rag", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None, help="每臂题数上限（冒烟用）")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    lock_path = args.output / "inference.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another fullscale run is active")
        profiles = {"tg": load_profile(args.profile_tg), "lex": load_profile(args.profile_rag)}
        if profiles["tg"].get("corpus_directories") or not profiles["lex"].get("corpus_directories"):
            raise ValueError("tg profile must be retrieval-free; rag profile must carry corpus_directories")
        sources = {name: digest(ROOT / name) for name in SOURCES}
        rows_in = [{"question_id": f"{args.task}_{index:04d}", "instruction": record["instruction"],
                    "question": record["question"], "reference": record["answer"]}
                   for index, record in enumerate(load_task(args.task))]
        if args.limit is not None:
            rows_in = rows_in[:args.limit]
        expected = {row["question_id"]: hashlib.sha256(json.dumps(
            [row["question_id"], row["instruction"], row["question"], row["reference"]],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest() for row in rows_in}
        report = {"task": args.task, "questions": len(rows_in), "sources": sources, "arms": {}}
        for arm in ("tg", "lex"):
            rows = run_arm(arm, args.task, rows_in, expected, profiles[arm], args.output, sources)
            report["arms"][arm] = {"mean": sum(r["score"] for r in rows) / len(rows),
                                   "errors": sum(1 for r in rows if r["error"]),
                                   "truncations": sum(1 for r in rows if r.get("finish_reason") == "length")}
            (args.output / arm / "detailed_results.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "sources"},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
