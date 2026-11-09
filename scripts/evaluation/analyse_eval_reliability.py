"""Describe recorded repeats and model cost, without treating repeats as new cases.

This is offline analysis of an immutable ablation ledger. It measures recorded
pass/outcome variation, not semantic agreement or human annotation agreement.
Unknown usage remains null; failed attempts remain in every planned denominator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.evaluation.ablation_analysis import load_attempts  # noqa: E402


def number(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def metric(values: list[float | None]) -> dict[str, Any]:
    known = sorted(value for value in values if value is not None)
    complete = len(known) == len(values) and bool(values)
    return {
        "records": len(values), "known_records": len(known), "missing_records": len(values) - len(known),
        "known_sum": sum(known) if known else None,
        "complete_sum": sum(known) if complete else None,
        "known_mean": statistics.mean(known) if known else None,
        "complete_mean": statistics.mean(known) if complete else None,
        "known_p50": statistics.median(known) if known else None,
        "known_p95": known[math.ceil(len(known) * .95) - 1] if known else None,
    }


def cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = []
    for row in rows:
        incoming, outgoing = number(row.get("input_tokens")), number(row.get("output_tokens"))
        totals.append(incoming + outgoing if incoming is not None and outgoing is not None else None)
    return {
        "input_tokens": metric([number(row.get("input_tokens")) for row in rows]),
        "output_tokens": metric([number(row.get("output_tokens")) for row in rows]),
        "total_tokens": metric(totals),
        "provider_latency_ms": metric([number(row.get("latency_ms")) for row in rows]),
        "attempt_elapsed_seconds": metric([number(row.get("elapsed_seconds")) for row in rows]),
    }


def case_interval(items: list[dict[str, Any]], iterations: int, seed: int) -> dict[str, Any]:
    """Exploratory interval over case means, after averaging repeats per question."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for item in items:
        if item["case_id"] is None:
            return {"method": "not_computed", "reason": "missing_case_identity", "ci95": None}
        buckets[str(item["case_id"])].append(item["recorded_pass_rate"])
    means = [statistics.mean(values) for _, values in sorted(buckets.items())]
    if len(means) < 2:
        return {"method": "not_computed", "reason": "fewer_than_two_case_clusters",
                "case_clusters": len(means), "ci95": None}
    rng = random.Random(seed)
    sampled = sorted(statistics.mean(rng.choices(means, k=len(means))) for _ in range(iterations))
    return {
        "method": "exploratory_case_cluster_percentile_bootstrap", "case_clusters": len(means),
        "independent_question_ids": len(items), "iterations": iterations, "seed": seed,
        "estimand": "equal-weight mean of case means of per-question recorded pass rates",
        "point": statistics.mean(means),
        "ci95": [sampled[max(0, math.ceil(iterations * .025) - 1)],
                 sampled[max(0, math.ceil(iterations * .975) - 1)]],
        "population_inference_validated": False,
        "notice": "Small authored case set; descriptive interval, not a powered population or legal-quality claim.",
    }


def analyse(rows: list[dict[str, Any]], *, expected_repeats: int = 5,
            iterations: int = 2000, seed: int = 20261029) -> dict[str, Any]:
    if not rows or expected_repeats < 2 or iterations < 100:
        raise ValueError("Require a nonempty ledger, expected_repeats >= 2 and iterations >= 100")
    seen = set()
    identity: dict[str, tuple[Any, Any]] = {}
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        item, group, repeat = row["item"], row["group"], row.get("repeat")
        if not isinstance(item, str) or not item or type(repeat) is not int or repeat < 0:
            raise ValueError("Invalid question/repeat identity")
        if type(row.get("passed")) is not bool or type(row.get("executed")) is not bool:
            raise ValueError("Recorded passed/executed flags must be booleans")
        if not isinstance(row.get("outcome"), str) or not row["outcome"]:
            raise ValueError("Recorded outcome must be a nonempty string")
        if row.get("raw_answer") is not None and not isinstance(row["raw_answer"], str):
            raise ValueError("Recorded raw_answer must be a string or null")
        has_answer = isinstance(row.get("raw_answer"), str) and bool(row["raw_answer"].strip())
        if ((row.get("outcome") == "ok" and (not row["executed"] or not has_answer))
                or (row["passed"] and row.get("outcome") != "ok")):
            raise ValueError("Inconsistent execution/outcome/pass/answer record")
        key = (group, item, repeat)
        if key in seen:
            raise ValueError(f"Duplicate attempt: {key}")
        seen.add(key)
        metadata = (row.get("case_id"), row.get("category"))
        if item in identity and identity[item] != metadata:
            raise ValueError(f"Question changed case/category identity: {item}")
        identity[item] = metadata
        grouped[group][item].append(row)
    groups = {}
    for group, questions in sorted(grouped.items()):
        items = []
        group_rows = []
        for item, samples in sorted(questions.items()):
            samples.sort(key=lambda sample: sample["repeat"])
            group_rows.extend(samples)
            passes = [sample["outcome"] == "ok" and sample["passed"] for sample in samples]
            answers = [sample for sample in samples if sample["executed"] and sample.get("raw_answer")]
            repeated = len(samples) >= 2
            items.append({
                "item": item, "case_id": identity[item][0], "category": identity[item][1],
                "repeat_ids": [sample["repeat"] for sample in samples], "recorded_attempts": len(samples),
                "requested_repeats_complete": [sample["repeat"] for sample in samples] == list(range(expected_repeats)),
                "recorded_pass_rate": statistics.mean(passes),
                "recorded_pass_variance": statistics.pvariance(passes) if repeated else None,
                "recorded_pass_stable": len(set(passes)) == 1 if repeated else None,
                "recorded_outcome_stable": len({sample["outcome"] for sample in samples}) == 1 if repeated else None,
                "outcomes": dict(sorted(Counter(sample["outcome"] for sample in samples).items())),
                "executed_model_answers": len(answers), "has_repeated_model_answers": len(answers) >= 2,
                "raw_answer_digest_count": len({hashlib.sha256(sample["raw_answer"].encode()).hexdigest()
                                               for sample in answers}),
                "cost_all_recorded_attempts": cost(samples),
            })
        executed = [row for row in group_rows if row["executed"]]
        successful = sum(row["outcome"] == "ok" and row["passed"] for row in group_rows)
        executed_cost = cost(executed)
        token_sum = executed_cost["total_tokens"]["complete_sum"]
        groups[group] = {
            "planned_recorded_attempts": len(group_rows), "executed_attempts": len(executed),
            "unique_question_ids": len(items),
            "case_clusters": len({item["case_id"] for item in items if item["case_id"] is not None}),
            "recorded_model_ids": sorted({str(row["model"]) for row in group_rows if row.get("model")}),
            "outcomes": dict(sorted(Counter(row["outcome"] for row in group_rows).items())),
            "passed_attempts": successful, "recorded_pass_rate": successful / len(group_rows),
            "questions_with_two_or_more_records": sum(item["recorded_attempts"] >= 2 for item in items),
            "questions_with_repeated_model_answers": sum(item["has_repeated_model_answers"] for item in items),
            "questions_with_requested_repeats": sum(item["requested_repeats_complete"] for item in items),
            "questions_with_pass_flips": sum(item["recorded_pass_stable"] is False for item in items),
            "questions_with_outcome_changes": sum(item["recorded_outcome_stable"] is False for item in items),
            "questions_without_generated_answers": sum(item["executed_model_answers"] == 0 for item in items),
            "observed_repeat_counts": dict(sorted(Counter(item["recorded_attempts"] for item in items).items())),
            "cost_all_recorded_attempts": cost(group_rows), "cost_executed_including_failures": executed_cost,
            "executed_tokens_per_passed_attempt": token_sum / successful
            if token_sum is not None and successful else None,
            "case_uncertainty": case_interval(items, iterations, seed), "per_question": items,
        }
    return {
        "analysis_version": "lexvault-recorded-reliability-v1", "requested_repeats": expected_repeats,
        "unique_question_ids": len(identity),
        "case_clusters": len({case for case, _ in identity.values() if case is not None}),
        "groups": groups, "semantic_consistency_measured": False, "human_agreement_measured": False,
        "notice": "Records and repeated calls are not new question/case samples. Failed and unexecuted records remain "
                  "in planned denominators; missing usage stays null. Pass/outcome stability is not answer correctness. "
                  "Raw-text digest variation is descriptive, not semantic disagreement. No hosted model was called.",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=Path, required=True)
    parser.add_argument("--expected-repeats", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = args.attempts.read_bytes()
        report = analyse(load_attempts(args.attempts), expected_repeats=args.expected_repeats,
                         iterations=args.iterations, seed=args.seed)
        if raw != args.attempts.read_bytes():
            raise ValueError("Source ledger changed during analysis")
        report |= {"source_ledger_sha256": hashlib.sha256(raw).hexdigest(), "source": str(args.attempts)}
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (OSError, ValueError, KeyError) as exc:
        print(f"Analysis failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "questions": report["unique_question_ids"],
                      "groups": list(report["groups"]), "requested_repeats": args.expected_repeats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
