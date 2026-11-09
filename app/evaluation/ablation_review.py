"""Three-group blind review package: hide the group, keep the binding.

The package is built only from recorded attempts. An item enters the sample when
every requested group has a successful attempt for the same repeat; otherwise it
is reported as ineligible instead of being filled with a stand-in answer.

Sampling is frozen the same way as the answer-level blind review: two items per
category in digest order, then the rest by digest order, filling uncovered cases
first when the dataset covers several cases. Group labels are replaced by
X1..Xn in a seeded order; the mapping lives in a separate key file.
"""
from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from app.core.digest import json_sha256
from app.evaluation.answer_eval import score_answer_v2

BLIND_VERSION = "ablation-blind-v1"
SAMPLE_RULE = "two-per-category-case-coverage-then-sha256-order-v1"
MIN_SAMPLE = 20
COLLECTION_MODES = ("real_model", "stub")


def eligible_items(rows: Iterable[dict[str, Any]], groups: Iterable[str]) -> dict[str, int]:
    """Items with an ``ok`` attempt for every group, plus the earliest such repeat."""
    wanted = tuple(groups)
    if not wanted or len(set(wanted)) != len(wanted):
        raise ValueError("分组必须非空且不重复")
    per_item: dict[str, dict[int, dict[str, dict[str, Any]]]] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        if row.get("group") in wanted:
            per_item[str(row["item"])][int(row.get("repeat", 0))][str(row["group"])] = row
    eligible: dict[str, int] = {}
    for item, repeats in per_item.items():
        for repeat in sorted(repeats):
            slots = repeats[repeat]
            if all(group in slots and slots[group].get("outcome") == "ok" for group in wanted):
                eligible[item] = repeat
                break
    return eligible


def sample_ids(dataset: dict[str, Any], eligible: dict[str, int], size: int = MIN_SAMPLE) -> list[str]:
    """Stratified deterministic sample; ineligible items are never substituted."""
    items = {item["id"]: item for item in dataset["items"]}
    missing = sorted(set(eligible) - set(items))
    if missing:
        raise ValueError(f"账本中的题目不在数据集里：{missing[:3]}")
    pool = [items[item_id] for item_id in eligible]
    if size < MIN_SAMPLE:
        raise ValueError(f"盲审样本至少 {MIN_SAMPLE} 题")
    if size > len(pool):
        raise ValueError(f"可用题目只有 {len(pool)} 道，少于要求的 {size} 道")
    ordered = sorted(pool, key=lambda item: json_sha256([dataset["source_sha256"], item["id"]]))
    selected: list[str] = []
    for category in sorted({item["category"] for item in pool}):
        selected.extend(item["id"] for item in [row for row in ordered if row["category"] == category][:2])
    covered = {str(items[item_id].get("case_id")) for item_id in selected}
    remaining = [item for item in ordered if item["id"] not in selected]
    # A holdout spanning several cases should not be decided by one case alone.
    remaining.sort(key=lambda item: (str(item.get("case_id")) in covered, json_sha256(item["id"])))
    selected.extend(item["id"] for item in remaining)
    return selected[:size]


def _presentation_order(groups: tuple[str, ...], item_id: str, seed: int) -> list[str]:
    order = list(groups)
    random.Random(f"{seed}:{item_id}").shuffle(order)
    return order


def build_package(
    dataset: dict[str, Any], rows: list[dict[str, Any]], *, groups: tuple[str, ...] = ("A", "B", "C"),
    size: int = MIN_SAMPLE, seed: int = 20261027, collection_mode: str = "real_model",
    allow_stub: bool = False,
) -> dict[str, Any]:
    """Return ``review`` (reviewer-facing), ``key`` (hidden mapping) and ``manifest``."""
    if collection_mode not in COLLECTION_MODES:
        raise ValueError(f"未登记的采集方式：{collection_mode}")
    if collection_mode == "stub" and not allow_stub:
        raise ValueError("替身答案不能生成盲审包；确需演练请显式加 --allow-stub 并保留替身标记")
    attempts_sha256 = json_sha256(rows)
    eligible = eligible_items(rows, groups)
    if not eligible:
        raise ValueError("没有三组都成功的题目，不能生成盲审包")
    chosen = sample_ids(dataset, eligible, size)
    items = {item["id"]: item for item in dataset["items"]}
    sources = {(source["document_name"], source["page"]): source["text"] for source in dataset["sources"]}
    review_items: list[dict[str, Any]] = []
    key_items: dict[str, dict[str, str]] = {}
    for item_id in chosen:
        repeat = eligible[item_id]
        item = items[item_id]
        slots = {row["group"]: row for row in rows
                 if row["item"] == item_id and int(row.get("repeat", 0)) == repeat
                 and row["group"] in groups}
        order = _presentation_order(tuple(groups), item_id, seed)
        mapping = {f"X{index + 1}": group for index, group in enumerate(order)}
        review_items.append({
            "id": item_id, "category": item["category"], "case_id": item.get("case_id"),
            "question": item["question"], "case_description": item.get("case_description"),
            "rubric": {"must_include": item.get("must_include", []),
                       "must_not_claim": item.get("must_not_claim", []),
                       "acceptable_pages": item.get("acceptable_pages", []),
                       "expect_refusal": item.get("expect_refusal", False)},
            "sources": [{"document_name": name, "page": page, "text": sources[(name, page)]}
                        for name, page in item.get("acceptable_pages", []) if (name, page) in sources],
            "candidates": [{"label": label, "answer": str(slots[group].get("final_answer") or "").strip()}
                           for label, group in mapping.items()],
            "repeat": repeat,
        })
        key_items[item_id] = mapping
    review = {
        "schema_version": 1, "blind_version": BLIND_VERSION, "collection_mode": collection_mode,
        "notice": ("三组答案已隐藏组别并随机呈现；请按要点完整性、无据断言、引用准确与正确拒答四项独立判断。"
                   + ("本包使用替身答案，只用于流程演练，不得作为质量证据。"
                      if collection_mode == "stub" else "")),
        "dataset_sha256": json_sha256(dataset), "attempts_sha256": attempts_sha256,
        "groups_hidden": True, "sample_rule": SAMPLE_RULE, "items": review_items,
    }
    key = {"schema_version": 1, "blind_version": BLIND_VERSION,
           "dataset_sha256": json_sha256(dataset), "attempts_sha256": attempts_sha256,
           "items": key_items,
           "sample_sha256": json_sha256([item["id"] for item in review_items])}
    review["sample_sha256"] = key["sample_sha256"]
    manifest = {
        "schema_version": 1, "blind_version": BLIND_VERSION, "collection_mode": collection_mode,
        "sample_rule": SAMPLE_RULE, "seed": seed, "groups": list(groups),
        "items": len(review_items), "cases": len({str(item.get("case_id")) for item in review_items}),
        "eligible_pool": len(eligible),
        "dataset_sha256": json_sha256(dataset), "attempts_sha256": attempts_sha256,
        "review_sha256": json_sha256(review), "key_sha256": json_sha256(key),
        "notes": ["review.json 不含组别映射；key.json 单独保管",
                  "没有三组成功尝试的题目不进样本，也不补造答案"],
    }
    return {"review": review, "key": key, "manifest": manifest}


def preview_scores(dataset: dict[str, Any], review: dict[str, Any], key: dict[str, Any]) -> dict[str, Any]:
    """Referee-side integrity check: labels must map back to the recorded groups."""
    items = {item["id"]: item for item in dataset["items"]}
    rows: list[dict[str, Any]] = []
    for entry in review["items"]:
        mapping = key["items"][entry["id"]]
        if set(mapping) != {candidate["label"] for candidate in entry["candidates"]}:
            raise ValueError(f"{entry['id']}: 候选标签与映射不一致")
        for candidate in entry["candidates"]:
            score = score_answer_v2(items[entry["id"]], candidate["answer"])
            rows.append({"id": entry["id"], "label": candidate["label"],
                         "group": mapping[candidate["label"]], "passed": bool(score["passed"])})
    return {"candidates": len(rows), "rows": rows}
