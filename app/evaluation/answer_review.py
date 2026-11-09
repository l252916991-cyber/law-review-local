"""Frozen blind-review sampling and strict judge/human agreement.

Human labels are user-supplied attestations, never inferred from automatic scores.
"""
from __future__ import annotations

from typing import Any

from app.core.digest import json_sha256
from app.evaluation.answer_eval import cohen_kappa


def review_manifest(dataset: dict[str, Any], answers: dict[str, Any], size: int = 20) -> dict[str, Any]:
    items = dataset["items"]
    if not 20 <= size <= len(items):
        raise ValueError("盲审样本至少 20 题且不能超过数据集")
    ordered = sorted(items, key=lambda item: json_sha256([dataset["source_sha256"], item["id"]]))
    selected: list[str] = []
    for category in sorted({item["category"] for item in items}):
        selected.extend(item["id"] for item in [row for row in ordered if row["category"] == category][:2])
    selected.extend(item["id"] for item in ordered if item["id"] not in selected)
    value = {"version": 1, "dataset_sha256": json_sha256(dataset), "answers_sha256": json_sha256(answers),
             "sample_ids": selected[:size], "sampling": "two-per-category-then-sha256-order-v1"}
    return {**value, "sample_sha256": json_sha256(value)}


def human_agreement(manifest: dict[str, Any], judges: list[dict[str, Any]], human: dict[str, Any] | None) -> dict[str, Any]:
    pending: dict[str, Any] = {"status": "missing_human_labels", "kappa": None, "items": 0,
                               "label_source": "user_supplied_independent_human_attestation"}
    if human is None:
        return pending
    if human.get("sample_sha256") != manifest["sample_sha256"]:
        raise ValueError("人工标签不属于该冻结样本或答案版本")
    if human.get("attestation") != "independent_human" or not isinstance(human.get("reviewer"), str) or not human["reviewer"].strip():
        raise ValueError("人工文件必须声明独立人工来源及非空 reviewer")
    ids = manifest["sample_ids"]
    labels = human.get("labels")
    if not isinstance(labels, dict) or set(labels) != set(ids) or len(ids) < 20:
        raise ValueError("人工标签必须完整覆盖至少 20 个冻结样本，不能挑选通过题")
    if any(not isinstance(labels[key], dict) or type(labels[key].get("passed")) is not bool for key in ids):
        raise ValueError("人工 passed 标签必须全部为布尔值，不能为字符串或 null")
    rows = {row["id"]: row for row in judges}
    if any(key not in rows or rows[key].get("semantic_entailment_checked") is not True
           or type(rows[key].get("passed")) is not bool for key in ids):
        return {**pending, "status": "judge_incomplete"}
    left = [rows[key]["passed"] for key in ids]
    right = [labels[key]["passed"] for key in ids]
    kappa = cohen_kappa(left, right)
    return {"status": "degenerate_labels" if kappa is None else "computed",
            "items": len(ids), "kappa": kappa,
            "agreement": sum(a == b for a, b in zip(left, right)) / len(ids),
            "judge_labels": dict(zip(ids, left)), "human_labels": dict(zip(ids, right)),
            "disagreements": [key for key, a, b in zip(ids, left, right) if a != b],
            "label_source": pending["label_source"], "attestation_verified_independently": False}
