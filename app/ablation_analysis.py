"""Paired analysis for the A/B/C ablation ledger.

This layer only reads recorded attempts. It never re-scores an answer, never
calls a model, and never fills a missing group with a stand-in: an item that one
group never attempted is reported as unpaired instead of silently dropped.

Reported quantities follow the frozen protocol: same-question pairing, per
category, per case, repeat variation, and cost. Intervals are case-level
bootstrap over the recorded cases, which is why a six-case holdout cannot be
presented as a population estimate or an SLA.
"""
from __future__ import annotations

import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

GROUPS = ("A", "B", "C")
REQUIRED_FIELDS = ("item", "group", "outcome", "passed")
DEFAULT_SEED = 20261027
DEFAULT_ITERATIONS = 2000


def load_attempts(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL ledger; a malformed line fails loudly instead of being skipped."""
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"第 {number} 行不是合法 JSON：{exc}") from exc
        if not isinstance(row, dict) or any(field not in row for field in REQUIRED_FIELDS):
            raise ValueError(f"第 {number} 行缺少必需字段 {REQUIRED_FIELDS}")
        if row["group"] not in GROUPS:
            raise ValueError(f"第 {number} 行的分组未注册：{row['group']}")
        rows.append(row)
    if not rows:
        raise ValueError("账本为空")
    return rows


def _key(row: dict[str, Any]) -> tuple[str, int]:
    return str(row["item"]), int(row.get("repeat", 0))


def pair_attempts(
    rows: Iterable[dict[str, Any]], baseline: str, compare: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair by (item, repeat); a row without its counterpart is reported, not dropped."""
    for group in (baseline, compare):
        if group not in GROUPS:
            raise ValueError(f"未注册的分组：{group}")
    if baseline == compare:
        raise ValueError("对照分组不能相同")
    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[_key(row)][row["group"]] = row
    pairs: list[dict[str, Any]] = []
    unpaired: list[dict[str, Any]] = []
    for key in sorted(grouped):
        slots = grouped[key]
        if baseline in slots and compare in slots:
            pairs.append({"item": key[0], "repeat": key[1],
                          "baseline": slots[baseline], "compare": slots[compare]})
        else:
            unpaired.append({"item": key[0], "repeat": key[1],
                             "present": sorted(slots), "missing": sorted({baseline, compare} - set(slots))})
    if not pairs:
        raise ValueError("没有可配对的结果；请检查分组是否都已执行")
    return pairs, unpaired


def _cell(pairs: list[dict[str, Any]], group: str) -> dict[str, int]:
    ok = [pair for pair in pairs if pair[group]["outcome"] == "ok"]
    passed = sum(1 for pair in pairs if pair[group].get("passed"))
    return {"planned": len(pairs), "ok": len(ok), "passed": passed,
            "failed_or_refused": len(pairs) - len(ok)}


def mcnemar_exact(baseline_only: int, compare_only: int) -> dict[str, Any]:
    """Two-sided exact McNemar test on the discordant pairs (no scipy dependency)."""
    discordant = baseline_only + compare_only
    if discordant == 0:
        return {"baseline_only": 0, "compare_only": 0, "discordant": 0, "p_value": None}
    tail = sum(math.comb(discordant, index) for index in range(min(baseline_only, compare_only) + 1))
    p_value = min(1.0, 2 * tail / 2 ** discordant)
    return {"baseline_only": baseline_only, "compare_only": compare_only,
            "discordant": discordant, "p_value": round(p_value, 6)}


def _delta(pair: dict[str, Any]) -> int:
    return int(bool(pair["compare"].get("passed"))) - int(bool(pair["baseline"].get("passed")))


def _aggregate(pairs: list[dict[str, Any]], baseline: str, compare: str) -> dict[str, Any]:
    baseline_cell = _cell(pairs, "baseline")
    compare_cell = _cell(pairs, "compare")
    deltas = [_delta(pair) for pair in pairs]
    compare_only = sum(1 for value in deltas if value == 1)
    baseline_only = sum(1 for value in deltas if value == -1)
    return {
        "pairs": len(pairs),
        "baseline": baseline_cell, "compare": compare_cell,
        "pass_rate_baseline": round(baseline_cell["passed"] / len(pairs), 4),
        "pass_rate_compare": round(compare_cell["passed"] / len(pairs), 4),
        "delta_pass_rate": round(sum(deltas) / len(pairs), 4),
        "changed": sum(1 for value in deltas if value != 0),
        "mcnemar": mcnemar_exact(baseline_only, compare_only),
        "groups": [baseline, compare],
    }


def _by_category(pairs: list[dict[str, Any]], baseline: str, compare: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        buckets[str(pair["baseline"].get("category") or "")].append(pair)
    return {category: _aggregate(bucket, baseline, compare) for category, bucket in sorted(buckets.items())}


def _by_case(pairs: list[dict[str, Any]], baseline: str, compare: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        buckets[str(pair["baseline"].get("case_id"))].append(pair)
    return {case: _aggregate(bucket, baseline, compare) for case, bucket in sorted(buckets.items())}


def repeat_variation(rows: Iterable[dict[str, Any]], group: str) -> dict[str, Any]:
    """How often the same group flips between pass and fail across repeats."""
    outcomes: dict[str, set[bool]] = defaultdict(set)
    repeats: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["group"] != group:
            continue
        outcomes[str(row["item"])].add(bool(row.get("passed")))
        repeats[str(row["item"])] += 1
    flips = sum(1 for values in outcomes.values() if len(values) > 1)
    return {"group": group, "items": len(outcomes), "items_with_flips": flips,
            "max_repeats": max(repeats.values()) if repeats else 0,
            "stable_items": len(outcomes) - flips}


def case_bootstrap(
    pairs: list[dict[str, Any]], small_sample_warning: bool = True, *,
    iterations: int = DEFAULT_ITERATIONS, seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Case-level bootstrap over the recorded cases, with the case count kept visible."""
    per_case: dict[str, list[int]] = defaultdict(list)
    for pair in pairs:
        case_id = pair["baseline"].get("case_id")
        per_case["" if case_id is None else str(case_id)].append(_delta(pair))
    if "" in per_case:
        return {"cases": 0, "delta": None, "interval": None, "iterations": 0, "seed": seed,
                "note": "账本缺少 case_id，无法按案例重采样"}
    case_means = [sum(values) / len(values) for _, values in sorted(per_case.items())]
    if not case_means:
        return {"cases": 0, "delta": None, "interval": None, "iterations": 0, "seed": seed,
                "note": "没有可分组的案例"}
    rng = random.Random(seed)
    samples = sorted(statistics.fmean(rng.choices(case_means, k=len(case_means)))
                     for _ in range(iterations))
    low = samples[max(0, int(0.025 * iterations) - 1)]
    high = samples[min(iterations - 1, int(0.975 * iterations))]
    return {
        "cases": len(case_means), "delta": round(statistics.fmean(case_means), 4),
        "interval": [round(low, 4), round(high, 4)], "iterations": iterations, "seed": seed,
        "note": ("案例数很少，区间只描述这一批案例的重采样波动，不能当作总体估计或 SLA"
                 if small_sample_warning else "按案例重采样"),
    }


def _cost(pairs: list[dict[str, Any]], baseline: str, compare: str) -> dict[str, Any]:
    """Token and latency deltas; unknown usage is counted, never filled with zero."""
    def values(slot: str, field: str) -> list[int]:
        known = [pair[slot][field] for pair in pairs
                 if pair[slot]["outcome"] == "ok" and isinstance(pair[slot].get(field), int)]
        return [int(value) for value in known]

    def token_delta(field: str) -> dict[str, Any]:
        known = [(pair["compare"].get(field), pair["baseline"].get(field)) for pair in pairs
                 if pair["compare"]["outcome"] == "ok" and pair["baseline"]["outcome"] == "ok"]
        complete = [c - b for c, b in known if isinstance(c, int) and isinstance(b, int)]
        baseline_values, compare_values = values("baseline", field), values("compare", field)
        return {"known_pairs": len(complete), "unknown_pairs": len(known) - len(complete),
                "median_delta": statistics.median(complete) if complete else None,
                "total_baseline": sum(baseline_values) if baseline_values else None,
                "total_compare": sum(compare_values) if compare_values else None}

    elapsed = [(pair["compare"].get("elapsed_seconds"), pair["baseline"].get("elapsed_seconds"))
               for pair in pairs if pair["compare"]["outcome"] == "ok" and pair["baseline"]["outcome"] == "ok"]
    deltas = [c - b for c, b in elapsed if isinstance(c, (int, float)) and isinstance(b, (int, float))]
    return {"input_tokens": token_delta("input_tokens"), "output_tokens": token_delta("output_tokens"),
            "elapsed_seconds": {"known_pairs": len(deltas),
                                "median_delta": round(statistics.median(deltas), 3) if deltas else None},
            "unknown_usage_pairs": sum(1 for pair in pairs
                                       if not pair["compare"].get("usage_known")
                                       or not pair["baseline"].get("usage_known"))}


def analyse(
    rows: list[dict[str, Any]], baseline: str = "A", compare: str = "C", *,
    iterations: int = DEFAULT_ITERATIONS, seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Build the paired report; failed or refused attempts stay in every denominator."""
    pairs, unpaired = pair_attempts(rows, baseline, compare)
    return {
        "schema_version": 1,
        "groups": {"baseline": baseline, "compare": compare},
        "paired": len(pairs), "unpaired": unpaired,
        "summary": _aggregate(pairs, baseline, compare),
        "by_category": _by_category(pairs, baseline, compare),
        "by_case": _by_case(pairs, baseline, compare),
        "repeat_variation": {group: repeat_variation(rows, group) for group in (baseline, compare)},
        "case_bootstrap": case_bootstrap(pairs, iterations=iterations, seed=seed),
        "cost": _cost(pairs, baseline, compare),
        "notes": [
            "配对只比较同一题同一轮；未尝试、失败与规则拒答按未通过计入分母",
            "确定性评分与附录分开：通过判定取模型原始回答，附录只影响最终文本",
            "案例级区间描述本批波动，不构成总体估计、SLA 或法律准确率",
        ],
    }


def main_report(path: str | Path, baseline: str, compare: str, *, iterations: int = DEFAULT_ITERATIONS,
                seed: int = DEFAULT_SEED) -> dict[str, Any]:
    return analyse(load_attempts(path), baseline, compare, iterations=iterations, seed=seed)
