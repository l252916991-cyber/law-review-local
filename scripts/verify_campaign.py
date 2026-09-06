"""Offline verifier for score-85 experiment inputs, requests and fixed-v3 scores."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_metrics import score_lawbench_item
from app.benchmark_reporting import atomic_json, metrics
from scripts.benchmark_campaign import digest, read_rows, validate_campaign


def _legacy_postprocess_v1(task_id: str, question: str, prediction: str) -> tuple[str, dict[str, object]]:
    """Reconstruct v3/v4 experiments after the live postprocessor evolves."""
    if task_id != "2-1" or not prediction:
        return prediction, {"version": "benchmark-postprocess-v1", "applied": False}
    from app.benchmark_postprocess import preserve_correction_surface
    revised = preserve_correction_surface(question, prediction)
    return revised, {"version": "benchmark-postprocess-v1", "applied": revised != prediction,
                     "original_prediction": prediction if revised != prediction else None,
                     "policy": "source-surface-only; no reference access"}


def _legacy_postprocess_v2(task_id: str, question: str, prediction: str,
                           retrieval: dict[str, Any] | None) -> tuple[str, dict[str, object]]:
    """Reconstruct frozen experiment-v5 deterministic processing."""
    from app.benchmark_amount import crime_amount, format_amount
    from app.benchmark_postprocess import ARTICLE_HEADING, _source_order_triggers, preserve_correction_surface
    revised, policy = prediction, "unchanged"
    details: dict[str, object] = {}
    if task_id == "1-1" and retrieval and retrieval.get("mode") == "exact_article" and retrieval.get("hits"):
        revised = "\n".join(ARTICLE_HEADING.sub("", hit["text"], count=1) for hit in retrieval["hits"])
        policy = "exact-retrieved-article-content; no reference access"
        details["document_articles"] = [f"{hit['document_id']}/{hit['article_id']}" for hit in retrieval["hits"]]
    elif task_id == "2-1" and prediction:
        revised = preserve_correction_surface(question, prediction)
        policy = "source-surface-only; no reference access"
    elif task_id == "2-10" and prediction:
        revised = _source_order_triggers(question, prediction)
        policy = "deduplicate-and-order-verbatim-triggers-by-source; no reference access"
    elif task_id == "3-7":
        value, amount_audit = crime_amount(question)
        details["amount"] = amount_audit
        if value is not None:
            revised = format_amount(value)
            policy = "high-confidence-source-arithmetic-else-model-fallback; no reference access"
    applied = revised != prediction
    return revised, {"version": "benchmark-postprocess-v2", "applied": applied,
                     "original_prediction": prediction if applied else None, "policy": policy, **details}


def _legacy_postprocess_v3(task_id: str, question: str, prediction: str,
                           retrieval: dict[str, Any] | None) -> tuple[str, dict[str, object]]:
    """Reconstruct frozen experiment-v6 processing after label tools evolve."""
    revised, audit = _legacy_postprocess_v2(task_id, question, prediction, retrieval)
    audit["version"] = "benchmark-postprocess-v3"
    if task_id == "2-9":
        from app.benchmark_event_tools import event_labels
        labels = event_labels(question)
        if labels:
            revised = ";".join(labels)
            audit = {"version": "benchmark-postprocess-v3", "applied": revised != prediction,
                     "original_prediction": prediction if revised != prediction else None,
                     "policy": "public-event-ontology-lexicon; no reference access", "event_labels": labels}
    return revised, audit


def _legacy_postprocess_v4(task_id: str, question: str, prediction: str,
                           retrieval: dict[str, Any] | None) -> tuple[str, dict[str, object]]:
    """Reconstruct frozen experiment-v7 processing before summary extraction."""
    from app.benchmark_event_tools import event_labels
    from app.benchmark_postprocess import ARTICLE_HEADING, _source_order_triggers, preserve_correction_surface
    revised, policy = prediction, "unchanged"
    details: dict[str, object] = {}
    if task_id == "1-1" and retrieval and retrieval.get("mode") == "exact_article" and retrieval.get("hits"):
        revised = "\n".join(ARTICLE_HEADING.sub("", hit["text"], count=1) for hit in retrieval["hits"])
        policy = "exact-retrieved-article-content; no reference access"
        details["document_articles"] = [f"{hit['document_id']}/{hit['article_id']}" for hit in retrieval["hits"]]
    elif task_id == "2-1" and prediction:
        revised = preserve_correction_surface(question, prediction)
        policy = "source-surface-only; no reference access"
    elif task_id == "2-9":
        labels = event_labels(question)
        if labels:
            revised = ";".join(labels)
            policy = "public-event-ontology-lexicon; no reference access"
            details["event_labels"] = labels
    elif task_id == "2-10" and prediction:
        revised = _source_order_triggers(question, prediction)
        policy = "deduplicate-and-order-verbatim-triggers-by-source; no reference access"
    applied = revised != prediction
    return revised, {"version": "benchmark-postprocess-v4", "applied": applied,
                     "original_prediction": prediction if applied else None, "policy": policy, **details}


def verify(campaign_dir: Path, run_dir: Path, write: bool = True) -> dict[str, Any]:
    campaign = validate_campaign(campaign_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    summary = json.loads((run_dir / "summary.json").read_text())
    rows = read_rows(run_dir / "detailed_results.jsonl")
    inputs = {row["question_id"]: row for row in read_rows(campaign_dir / "inputs.jsonl")}
    references = {row["question_id"]: row["reference"] for row in read_rows(campaign_dir / "references.jsonl")}
    issues = []
    if digest(campaign_dir / "campaign.json") != manifest["campaign_sha256"]:
        issues.append("Campaign manifest mismatch")
    if manifest.get("experiment_version") in {2, 3, 4, 5, 6, 7, 8}:
        from scripts.benchmark_experiment import configuration
        if digest(run_dir / "profile.json") != manifest["profile_sha256"] or configuration(run_dir / "profile.json") != manifest["config"]:
            issues.append("External profile mismatch")
        from app.benchmark_retrieval import corpus_fingerprint
        if corpus_fingerprint(manifest["config"].get("corpus_directories", [])) != manifest["corpus_files"]:
            issues.append("Corpus fingerprint mismatch")
    elif manifest["config"] != campaign["profiles"][manifest["profile"]]:
        issues.append("Profile mismatch")
    if Counter(row["question_id"] for row in rows) != Counter(manifest["question_ids"]):
        issues.append("Missing, duplicate or unexpected results")
    known_sources = {"scripts/benchmark_campaign.py", "app/benchmark_solver.py", "app/services.py", "app/config.py"}
    if manifest.get("experiment_version") == 2:
        known_sources.update({"scripts/benchmark_experiment.py", "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py", "app/legal_corpus.py"})
    elif manifest.get("experiment_version") in {3, 4}:
        known_sources.update({"scripts/benchmark_experiment.py", "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py",
                              "app/benchmark_postprocess.py", "app/legal_corpus.py"})
    elif manifest.get("experiment_version") == 5:
        known_sources.update({"scripts/benchmark_experiment.py", "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py",
                              "app/benchmark_postprocess.py", "app/benchmark_amount.py", "app/legal_corpus.py"})
    elif manifest.get("experiment_version") == 6:
        known_sources.update({"scripts/benchmark_experiment.py", "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py",
                              "app/benchmark_postprocess.py", "app/benchmark_amount.py", "app/benchmark_event_tools.py",
                              "app/legal_corpus.py"})
    elif manifest.get("experiment_version") == 7:
        known_sources.update({"scripts/benchmark_experiment.py", "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py",
                              "app/benchmark_postprocess.py", "app/benchmark_event_tools.py", "app/legal_corpus.py"})
    elif manifest.get("experiment_version") == 8:
        known_sources.update({"scripts/benchmark_experiment.py", "app/benchmark_rag_solver.py", "app/benchmark_retrieval.py",
                              "app/benchmark_postprocess.py", "app/benchmark_event_tools.py",
                              "app/benchmark_summary_tools.py", "app/legal_corpus.py"})
    if set(manifest["sources"]) != known_sources:
        issues.append("Source snapshot coverage mismatch")
    for name, expected in manifest["sources"].items():
        if name not in known_sources or digest(run_dir / "source_snapshot" / name) != expected:
            issues.append(f"Source snapshot mismatch: {name}")
    if {path.stem for path in (run_dir / "checkpoints").glob("*.json")} != set(manifest["question_ids"]):
        issues.append("Checkpoint coverage mismatch")
    response_count = 0
    for row in rows:
        key = row["question_id"]
        original = inputs.get(key)
        if original is None:
            issues.append(f"Unexpected input: {key}")
            continue
        if any(row.get(name) != value for name, value in original.items()) or row["reference"] != references[key]:
            issues.append(f"Input/reference mismatch: {key}")
        if json.loads((run_dir / "checkpoints" / f"{key}.json").read_text()) != row:
            issues.append(f"Checkpoint mismatch: {key}")
        if not row.get("error"):
            scored = score_lawbench_item(row["task"], row["prediction"], references[key], question=original["question"]).to_dict()
            if any(row.get(name) != value for name, value in scored.items()):
                issues.append(f"Score mismatch: {key}")
        elif row["score"] != 0:
            issues.append(f"Technical failure not zero: {key}")
        calls = row.get("calls", [])
        if not calls:
            issues.append(f"No inference request: {key}")
        for call in calls:
            request = call["request"]
            encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            if hashlib.sha256(encoded).hexdigest() != call["request_sha256"]:
                issues.append(f"Request hash mismatch: {key}")
            for name in ("model", "temperature", "max_tokens"):
                if request[name] != manifest["config"][name]:
                    issues.append(f"Request configuration mismatch: {key}/{name}")
            if request["chat_template_kwargs"]["enable_thinking"] != manifest["config"]["enable_thinking"]:
                issues.append(f"Thinking mode mismatch: {key}")
            if request["messages"][1]["content"] != original["instruction"].strip() + "\n" + original["question"]:
                issues.append(f"Question prompt mismatch: {key}")
            if manifest["config"]["strategy"] == "statutory_rag":
                from app.benchmark_retrieval import retrieve
                context = retrieve(original["task"], original["question"], manifest["config"]["corpus_directories"])
                if row.get("retrieval") != context:
                    issues.append(f"Retrieval mismatch: {key}")
                expected_context = [{"role": "user", "content": context["context"]}] if context["context"] else []
                if request["messages"][2:] != expected_context:
                    issues.append(f"Statutory prompt mismatch: {key}")
            response_count += bool(call.get("finish_reason"))
        if calls:
            expected_prediction = calls[-1]["prediction"]
            if row.get("postprocess") is not None:
                experiment_version = manifest.get("experiment_version", 1)
                if experiment_version < 5:
                    expected_prediction, audit = _legacy_postprocess_v1(
                        row["task"], original["question"], expected_prediction,
                    )
                elif experiment_version == 5:
                    expected_prediction, audit = _legacy_postprocess_v2(
                        row["task"], original["question"], expected_prediction, row.get("retrieval"),
                    )
                elif experiment_version == 6:
                    expected_prediction, audit = _legacy_postprocess_v3(
                        row["task"], original["question"], expected_prediction, row.get("retrieval"),
                    )
                elif experiment_version == 7:
                    expected_prediction, audit = _legacy_postprocess_v4(
                        row["task"], original["question"], expected_prediction, row.get("retrieval"),
                    )
                else:
                    from app.benchmark_postprocess import postprocess
                    expected_prediction, audit = postprocess(
                        row["task"], original["question"], expected_prediction,
                        retrieval=row.get("retrieval"),
                    )
                if row["postprocess"] != audit:
                    issues.append(f"Postprocess mismatch: {key}")
            if row["prediction"] != expected_prediction:
                issues.append(f"Final output does not match recorded model answer and deterministic processing: {key}")
    recalculated = metrics(rows)
    for name, expected in recalculated.items():
        if summary.get(name) != expected:
            issues.append(f"Summary mismatch: {name}")
    if summary["status"] != "completed":
        issues.append("Run incomplete")
    result = {"valid": not issues, "issues": issues, "questions": len(rows), "responses_received": response_count,
              "artifacts": {name: digest(run_dir / name) for name in ("manifest.json", "summary.json", "detailed_results.jsonl")},
              "metrics": recalculated, "scope": "fixed-v3 inputs, snapshots, calls, outputs and score recomputation",
              "note": "Does not independently prove absence of benchmark contamination in model pretraining."}
    if write:
        atomic_json(run_dir / "verification.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    result = verify(args.campaign_dir, args.run_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
