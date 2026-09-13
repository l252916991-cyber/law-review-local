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


def _hash_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify(directory: Path, *, artifacts_only: bool = False, write: bool = True) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (directory / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    issues = []
    routed_protocol = "routing" in manifest
    manifest_config_hash = _hash_json({key: manifest[key] for key in (
        "protocol_version", "scorer_version", "postprocess", "retrieval", "prompt_strategy",
        "model_config", "retry", "routing",
    )}) if routed_protocol else None
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
        sample = planned[key]
        if routed_protocol:
            route = sample.get("route")
            if route not in {"legacy_prompt", "solver_task_guided_control", "statutory_rag"}:
                issues.append(f"Missing/invalid task route: {key}")
                continue
            retrieval = sample.get("retrieval")
            if route == "statutory_rag":
                required = manifest.get("routing", {}).get("task_3_2", {})
                if row.get("task") != "3-2" or required.get("ranker_policy") != "lexical" \
                        or required.get("solver_transport_retries") != 0:
                    issues.append(f"RAG route policy mismatch: {key}")
                if retrieval is None or _hash_json(retrieval) != sample.get("retrieval_hash") \
                        or row.get("retrieval") != retrieval or retrieval.get("ranker") != "lexical" \
                        or retrieval.get("ranker_policy") != "lexical":
                    issues.append(f"Retrieval provenance mismatch: {key}")
                if not artifacts_only:
                    from app.benchmark_retrieval import retrieve

                    current = retrieve(row["task"], row["question"], manifest["retrieval"]["corpus_directories"],
                                       ranker_policy="lexical")
                    if current != retrieval:
                        issues.append(f"Live retrieval mismatch: {key}")
            if artifacts_only:
                messages = row.get("request_messages")
            else:
                from unified_benchmark_runner import route_messages

                messages = route_messages(row, route, manifest.get("prompt_strategy", "task_guided"),
                                          manifest["model_config"], retrieval)
            if not isinstance(messages, list) or len(messages) < 2:
                issues.append(f"Missing request messages: {key}")
                messages = []
            system = messages[0].get("content") if messages else None
            prompt = messages[1].get("content") if len(messages) > 1 else None
            digest = _hash_json(messages)
            if row.get("task_route") != route or row.get("manifest_config_hash") != manifest_config_hash:
                issues.append(f"Route/config provenance mismatch: {key}")
            if row.get("request_messages") != messages:
                issues.append(f"Request messages mismatch: {key}")
            if route in {"solver_task_guided_control", "statutory_rag"}:
                from unified_benchmark_runner import solver_provenance_issues

                provenance = solver_provenance_issues(
                    row, route, messages, manifest["model_config"],
                    manifest["retrieval"]["corpus_directories"],
                )
                if provenance:
                    issues.append(f"Solver provenance mismatch: {key} ({', '.join(provenance)})")
        else:
            system, prompt = ((row["system_prompt"], row["user_prompt"]) if artifacts_only else
                              prompt_for(row, strategy=manifest.get("prompt_strategy", "task_guided")))
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
        elif row.get("score") != 0.0 or row.get("metric") != "error":
            issues.append(f"Failure must score zero: {key}")
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
