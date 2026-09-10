"""Paired offline statutory retrieval evaluation on frozen, caller-supplied gold."""
from __future__ import annotations

import math
import random
from statistics import mean
from typing import Any

from .statutory_hybrid import MODES, StatutoryHybrid
from .statutory_index import digest

METRICS = ("hit_at_5", "mrr_at_10", "ndcg_at_10")


def metrics(ids: list[str], gold: dict[str, int]) -> dict[str, float]:
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate retrieved IDs")
    relevant = [i for i, key in enumerate(ids[:10], 1) if gold.get(key, 0) > 0]
    dcg = sum((2 ** gold.get(key, 0) - 1) / math.log2(i + 1) for i, key in enumerate(ids[:10], 1))
    ideal = sum((2 ** grade - 1) / math.log2(i + 1)
                for i, grade in enumerate(sorted(gold.values(), reverse=True)[:10], 1))
    return {"hit_at_5": float(any(rank <= 5 for rank in relevant)),
            "mrr_at_10": 1 / relevant[0] if relevant else 0.0, "ndcg_at_10": dcg / ideal if ideal else 0.0}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo = math.floor(position)
    return ordered[lo] + (ordered[math.ceil(position)] - ordered[lo]) * (position - lo)


def paired_delta(values: list[float], *, samples: int, seed: int) -> dict[str, Any]:
    if not values:
        return {"n": 0, "delta": None, "ci95": None}
    rng = random.Random(seed)
    draws = [mean(rng.choices(values, k=len(values))) for _ in range(samples)]
    return {"n": len(values), "delta": mean(values), "ci95": [percentile(draws, .025), percentile(draws, .975)]}


def evaluate(retriever: StatutoryHybrid, dataset: dict[str, Any], config: dict[str, Any], *,
             split: str = "both") -> dict[str, Any]:
    """Evaluate both frozen splits; promotion requires their joint evidence."""
    if split == "both":
        dev = evaluate(retriever, dataset, config, split="dev")
        confirm = evaluate(retriever, dataset, config, split="confirm")
        return {"dev": dev, "confirm": confirm, "promotion": {
            mode: bool(dev["comparisons"][mode]["split_gate_passed"]
                       and confirm["comparisons"][mode]["split_gate_passed"]
                       and dev["experiment_fingerprint"] == confirm["experiment_fingerprint"])
            for mode in MODES[1:]}}
    tasks = dataset["tasks"]
    if split not in {"dev", "confirm"} or config["direction"] != "candidate_minus_lexical":
        raise ValueError("Invalid split or comparison direction")
    if config["bootstrap_samples"] < 100 or config["min_confirm_queries"] < 1:
        raise ValueError("Invalid acceptance configuration")
    ids = [task["id"] for task in tasks]
    queries = [task["query"].strip() for task in tasks]
    if len(ids) != len(set(ids)) or len(queries) != len(set(queries)):
        raise ValueError("Duplicate tasks or query leakage across splits")
    for task in tasks:
        gold = task["gold"]
        if (task["split"] not in {"dev", "confirm"} or not task["query"].strip()
                or not gold or not any(gold.values()) or set(gold) - set(retriever.index.rows)
                or any(type(grade) is not int or not 0 <= grade <= 3 for grade in gold.values())):
            raise ValueError("Invalid task split or gold")
        if not task.get("query_effective_date"):
            raise ValueError("Frozen benchmark requires query_effective_date")
        eligible, _ = retriever.index.eligible(explicit_law=task.get("explicit_law"),
                                               query_effective_date=task["query_effective_date"])
        if {key for key, grade in gold.items() if grade > 0} - set(eligible):
            raise ValueError("Positive gold is outside task law/date eligibility")
    selected = [task for task in tasks if task["split"] == split]
    if not selected:
        raise ValueError("Empty evaluation split")
    runs: dict[str, list[dict[str, Any]]] = {mode: [] for mode in MODES}
    for task in selected:
        for mode in MODES:
            result = retriever.search(task["query"], mode=mode, limit=10,
                                      explicit_law=task.get("explicit_law"), inferred_law=task.get("inferred_law"),
                                      query_effective_date=task["query_effective_date"])
            result["task_id"] = task["id"]
            result["metrics"] = metrics([hit["id"] for hit in result["hits"]], task["gold"]) if result["available"] else None
            runs[mode].append(result)
    summaries: dict[str, Any] = {}
    for mode_name, records in runs.items():
        successful = [record for record in records if record["available"]]
        summaries[mode_name] = {"queries": len(records), "available": len(successful),
                           "failure": len(records) - len(successful),
                           "metrics": {name: mean(r["metrics"][name] for r in successful) if successful else None for name in METRICS},
                           "p50_ms": percentile([r["latency_ms"] for r in records], .5),
                           "p95_ms": percentile([r["latency_ms"] for r in records], .95),
                           "embedding_calls_per_query": mean(r["embedding_calls"] for r in records),
                           "rerank_candidates_per_query": mean(r["rerank_candidates"] for r in records)}
    identity = {"dataset_sha256": digest(dataset), "config_sha256": digest(config),
                "index_fingerprint": retriever.index.fingerprint,
                "vectors_sha256": digest(retriever.index.vectors),
                "retrieval": {"candidates": retriever.candidates, "rrf_k": retriever.rrf_k,
                              "soft_boost": retriever.soft_boost},
                "embedding_model": getattr(retriever.embedding, "model", None),
                "reranker_model": getattr(retriever.reranker, "model", None)}
    identity_complete = bool(identity["embedding_model"] and identity["reranker_model"])
    comparisons: dict[str, Any] = {}
    for mode in MODES[1:]:
        pairs = [(a, b) for a, b in zip(runs["lexical"], runs[mode]) if a["available"] and b["available"]]
        comparisons[mode] = {name: paired_delta([b["metrics"][name] - a["metrics"][name] for a, b in pairs],
                                                samples=config["bootstrap_samples"], seed=config["seed"]) for name in METRICS}
        primary = comparisons[mode]["hit_at_5"]
        minimum = config["min_dev_queries"] if split == "dev" else config["min_confirm_queries"]
        direction_gate = (primary["delta"] is not None and (
            primary["delta"] >= config["min_dev_hit_delta"]
            and primary["ci95"][0] >= config["min_dev_hit_ci_lower"] if split == "dev"
            else primary["delta"] > 0))
        comparisons[mode]["split_gate_passed"] = bool(
            identity_complete and not dataset.get("fixture", False)
            and len(pairs) == len(selected) and len(pairs) >= minimum and direction_gate
            and comparisons[mode]["mrr_at_10"]["delta"] >= config["min_mrr_delta"]
            and comparisons[mode]["ndcg_at_10"]["delta"] >= config["min_ndcg_delta"]
            and summaries[mode]["p95_ms"] <= config["max_p95_ms"]
            and summaries[mode]["embedding_calls_per_query"] <= config["max_embedding_calls_per_query"]
            and summaries[mode]["rerank_candidates_per_query"] <= config["max_rerank_candidates_per_query"])
        comparisons[mode]["preset_gate_passed"] = False  # A single split can never promote.
    return {"dataset_sha256": digest(dataset), "config_sha256": digest(config),
            "experiment_identity": identity, "identity_complete": identity_complete,
            "experiment_fingerprint": digest(identity), "primary_metric": "hit_at_5",
            "index_fingerprint": retriever.index.fingerprint, "split": split,
            "direction": config["direction"], "fixture": dataset.get("fixture", False),
            "gate_notice": "Preset acceptance thresholds, not demonstrated benefits",
            "summaries": summaries, "comparisons": comparisons, "runs": runs}
