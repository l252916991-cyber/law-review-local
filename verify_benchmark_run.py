#!/usr/bin/env python3
"""Offline integrity gate for a completed run; never invokes the model."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from app.benchmark_metrics import score_lawbench_item
from app.benchmark_reporting import atomic_json, metrics, paired_baseline, prompt_for, source_hashes


def verify(directory: Path, *, artifacts_only: bool = False, write: bool = True) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (directory / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    issues = []
    planned = {r["question_id"]: r for r in manifest["samples"]}
    counts = Counter(r["question_id"] for r in rows)
    if summary["status"] not in {"completed", "completed_with_errors"}:
        issues.append("Run is not complete")
    if len(planned) != len(manifest["samples"]) or len(rows) != manifest["planned_questions"] or set(counts) != set(planned) or any(n != 1 for n in counts.values()):
        issues.append("Missing/duplicate/unexpected results")
    source_matches = source_hashes() == manifest["source_hashes"]
    if not artifacts_only and not source_matches:
        issues.append("Live scoring/prompt source differs from frozen manifest")
    for name, digest in manifest["source_hashes"].items():
        if name not in source_hashes():
            issues.append(f"Unexpected source snapshot: {name}")
            continue
        path = directory / "source_snapshot" / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            issues.append(f"Source snapshot mismatch: {name}")
    checkpoints = {p.stem for p in (directory / "checkpoints").glob("*.json")}
    if checkpoints != set(counts):
        issues.append("Checkpoint IDs differ from detailed results")
    for row in rows:
        key = row["question_id"]
        if key not in planned:
            continue
        path = directory / "checkpoints" / f"{key}.json"
        if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != row:
            issues.append(f"Checkpoint mismatch: {key}")
        system, prompt = (row["system_prompt"], row["user_prompt"]) if artifacts_only else prompt_for(row, strategy=manifest.get("prompt_strategy", "task_guided"))
        digest = hashlib.sha256(json.dumps((system, prompt), ensure_ascii=False).encode()).hexdigest()
        if (system, prompt) != (row["system_prompt"], row["user_prompt"]) or digest != planned[key]["prompt_hash"] or digest != row["prompt_hash"]:
            issues.append(f"Prompt mismatch: {key}")
        record = {name: row[name] for name in ("dataset", "task", "task_name", "question_id", "question", "instruction", "dataset_version", "reference", "question_hash")}
        record_digest = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        question_digest = hashlib.sha256(row["question"].encode()).hexdigest()
        if record_digest != planned[key]["record_hash"] or question_digest != planned[key]["question_hash"] or question_digest != row["question_hash"]:
            issues.append(f"Record mismatch: {key}")
        if row["scorer_version"] != manifest["scorer_version"] or row["prompt_version"] != manifest["protocol_version"]:
            issues.append(f"Protocol mismatch: {key}")
        if row["model_config"] != manifest["model_config"]:
            issues.append(f"Model config mismatch: {key}")
        if not row.get("error"):
            if not artifacts_only:
                rescored = score_lawbench_item(row["task"], row["prediction"], row["reference"], question=row["question"]).to_dict()
                if any(row.get(k) != v for k, v in rescored.items()):
                    issues.append(f"Score mismatch: {key}")
            if not row.get("finish_reason") or row.get("usage") is None:
                issues.append(f"Missing response metadata: {key}")
        if row.get("attempts", 0) < 1:
            issues.append(f"Missing model attempt: {key}")
    recalculated = metrics(rows)
    for key, value in recalculated.items():
        if summary.get(key) != value:
            issues.append(f"Summary mismatch: {key}")
    if manifest.get("baseline_results"):
        path = Path(manifest["baseline_results"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["baseline_sha256"]:
            issues.append("Baseline file changed")
        if not artifacts_only:
            comparison = paired_baseline(rows, path)
            if comparison != json.loads((directory / "paired_comparison.json").read_text(encoding="utf-8")):
                issues.append("Paired comparison mismatch")
    result = {"valid": not issues, "issues": issues, "questions": len(rows), "task_counts": dict(Counter(r["task"] for r in rows)), "metrics": recalculated,
              "scope": "artifacts_only" if artifacts_only else "artifacts_and_scoring",
              "live_source_matches": source_matches, "scoring_verified": not artifacts_only and source_matches and not issues,
              "note": "Integrity verifies reproducibility, not legal correctness or equivalence to official LawBench metrics."}
    if write:
        atomic_json(directory / ("artifact_verification.json" if artifacts_only else "verification.json"), result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--artifacts-only", action="store_true", help="Check saved artifacts after source changes; does NOT recheck historical scores")
    args = parser.parse_args()
    result = verify(args.run_dir, artifacts_only=args.artifacts_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["valid"] else 1)
