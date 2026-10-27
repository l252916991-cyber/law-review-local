"""Every frozen item must be passable by a compliant answer and fail a hollow one.

A benchmark that cannot be satisfied by following its own rubric turns every
model into a failure, so this is checked for both the development set and the
six-case holdout. The check is deterministic and never calls a model.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.answer_eval import load_dataset, score_answer
from app.collaboration_ablation import load_batch_dataset, load_prepared, prepare_batch

ROOT = Path(__file__).resolve().parents[1]
DATASETS = {
    "dev-v2": ROOT / "benchmarks/answer_eval/v2/dataset.json",
    "holdout-v1": ROOT / "benchmarks/answer_eval/holdout-v1/dataset.json",
}
REFUSAL_ANSWER = "资料不足，无法核验，建议补充材料。请律师复核原文与上述结论。"


def reference_answer(item: dict) -> tuple[str, list[dict]]:
    if item["expect_refusal"]:
        return REFUSAL_ANSWER, []
    pages = item["acceptable_pages"]
    markers = "".join(f"[资料{index + 1}]" for index in range(len(pages))) or "[资料1]"
    citations = [{"document_name": name, "page": page} for name, page in pages]
    return "；".join(item["must_include"]) + markers + "。请律师复核原文与上述结论。", citations


@pytest.mark.parametrize("label", sorted(DATASETS))
def test_every_frozen_item_is_satisfiable_by_a_compliant_answer(label):
    dataset = load_dataset(DATASETS[label])
    failures = []
    for item in dataset["items"]:
        answer, citations = reference_answer(item)
        score = score_answer(item, answer, citations=citations)
        if not score["passed"]:
            failures.append((item["id"], score["coverage"], score["violations"],
                             score["citation_precision"], score["refused"]))
    assert failures == [], failures


@pytest.mark.parametrize("label", sorted(DATASETS))
def test_answerable_items_reject_an_answer_without_required_points(label):
    dataset = load_dataset(DATASETS[label])
    rejected = 0
    for item in dataset["items"]:
        if item["expect_refusal"]:
            continue
        citations = [{"document_name": name, "page": page} for name, page in item["acceptable_pages"]]
        score = score_answer(item, "材料显示了相关情况[资料1]。请律师复核原文与上述结论。",
                             citations=citations)
        assert score["passed"] is False, item["id"]
        rejected += 1
    assert rejected == sum(1 for item in dataset["items"] if not item["expect_refusal"])


@pytest.mark.parametrize("label", sorted(DATASETS))
def test_refusal_items_require_a_refusal(label):
    dataset = load_dataset(DATASETS[label])
    refusal_items = [item for item in dataset["items"] if item["expect_refusal"]]
    assert refusal_items
    for item in refusal_items:
        assert score_answer(item, REFUSAL_ANSWER)["passed"] is True, item["id"]
        confident = "相关事实已经确认[资料1]。请律师复核原文与上述结论。"
        assert score_answer(item, confident, citations=[{"document_name": "x", "page": 1}])["passed"] is False


def test_prepared_holdout_context_reaches_every_accepted_page(tmp_path):
    dataset = load_batch_dataset(DATASETS["holdout-v1"])
    prepare_batch(dataset, tmp_path / "batch")
    manifest, _ = load_prepared(tmp_path / "batch")
    unreachable = [entry["id"] for entry in manifest["items"] if not entry["citation_reachable"]]
    assert unreachable == []
    assert len(manifest["items"]) == len(dataset["items"])


def test_manifest_records_acceptance_size_per_item(tmp_path):
    dataset = load_batch_dataset(DATASETS["holdout-v1"])
    manifest = prepare_batch(dataset, tmp_path / "batch2", item_ids=["A-normal-01", "A-insufficient-01"])
    by_id = {entry["id"]: entry for entry in manifest["items"]}
    assert by_id["A-normal-01"]["acceptable_pages"] == 2
    assert by_id["A-insufficient-01"]["acceptable_pages"] == 0
    assert by_id["A-insufficient-01"]["citation_reachable"] is True
