"""Three-group blind package: hidden labels, frozen sample, explicit stub marking."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ablation_review import (
    build_package,
    content_digest,
    eligible_items,
    preview_scores,
    sample_ids,
)
from app.collaboration_ablation import load_batch_dataset
from scripts.build_ablation_blind_package import main

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "benchmarks/answer_eval/holdout-v1/dataset.json"


def attempt(item: str, group: str, category: str, case_id: int, *, repeat: int = 0,
            outcome: str = "ok", answer: str = "合成答案[资料1]。请律师复核原文与上述结论。"):
    return {"item": item, "group": group, "category": category, "case_id": case_id,
            "repeat": repeat, "outcome": outcome, "passed": outcome == "ok",
            "final_answer": answer, "usage_known": True, "input_tokens": 100, "output_tokens": 10,
            "elapsed_seconds": 1.0}


def full_ledger(dataset: dict) -> list[dict]:
    rows = []
    for item in dataset["items"]:
        for group in ("A", "B", "C"):
            rows.append(attempt(item["id"], group, item["category"], item["case_id"]))
    return rows


def test_sampling_is_stratified_deterministic_and_covers_cases():
    dataset = load_batch_dataset(DATASET)
    rows = full_ledger(dataset)
    eligible = eligible_items(rows, ("A", "B", "C"))
    assert len(eligible) == 24
    first = sample_ids(dataset, eligible, 20)
    assert first == sample_ids(dataset, eligible, 20)
    assert len(first) == 20
    items = {item["id"]: item for item in dataset["items"]}
    categories = [items[item_id]["category"] for item_id in first]
    pool_counts = {category: sum(1 for item in dataset["items"] if item["category"] == category)
                   for category in {item["category"] for item in dataset["items"]}}
    for category, available in pool_counts.items():
        expected = min(2, available)
        assert categories.count(category) >= expected, (category, available)
    assert len({items[item_id]["case_id"] for item_id in first}) >= 5


def test_an_item_missing_one_group_is_ineligible_and_never_substituted():
    dataset = load_batch_dataset(DATASET)
    rows = [row for row in full_ledger(dataset) if not (row["group"] == "C" and row["item"] == "A-normal-01")]
    eligible = eligible_items(rows, ("A", "B", "C"))
    assert "A-normal-01" not in eligible
    with pytest.raises(ValueError, match="可用题目只有"):
        sample_ids(dataset, eligible, 24)


def test_package_hides_groups_and_keeps_the_mapping_in_the_key():
    dataset = load_batch_dataset(DATASET)
    package = build_package(dataset, full_ledger(dataset), size=20)
    review, key = package["review"], package["key"]
    assert review["groups_hidden"] is True
    assert len(review["items"]) == 20
    for entry in review["items"]:
        labels = [candidate["label"] for candidate in entry["candidates"]]
        assert labels == ["X1", "X2", "X3"]
        assert "group" not in json.dumps(entry["candidates"])
        answers = [candidate["answer"] for candidate in entry["candidates"]]
        assert all(answer for answer in answers)
        assert set(key["items"][entry["id"]]) == set(labels)
        assert sorted(key["items"][entry["id"]].values()) == ["A", "B", "C"]
    assert review["sample_sha256"] == key["sample_sha256"]
    assert review["dataset_sha256"] == content_digest(dataset)


def test_label_order_is_seeded_and_may_differ_between_items():
    dataset = load_batch_dataset(DATASET)
    rows = full_ledger(dataset)
    first = build_package(dataset, rows, size=20, seed=1)["key"]["items"]
    second = build_package(dataset, rows, size=20, seed=2)["key"]["items"]
    assert first != second
    orders = {tuple(sorted(mapping, key=mapping.get)) for mapping in first.values()}
    assert len(orders) > 1  # a fixed A,B,C order would defeat the blind review
    assert build_package(dataset, rows, size=20, seed=1)["key"]["items"] == first


def test_stub_answers_require_an_explicit_flag_and_stay_labeled():
    dataset = load_batch_dataset(DATASET)
    rows = full_ledger(dataset)
    with pytest.raises(ValueError, match="替身答案"):
        build_package(dataset, rows, size=20, collection_mode="stub")
    package = build_package(dataset, rows, size=20, collection_mode="stub", allow_stub=True)
    assert package["manifest"]["collection_mode"] == "stub"
    assert "替身答案" in package["review"]["notice"]


def test_preview_scores_map_labels_back_to_recorded_groups():
    dataset = load_batch_dataset(DATASET)
    package = build_package(dataset, full_ledger(dataset), size=20)
    preview = preview_scores(dataset, package["review"], package["key"])
    assert preview["candidates"] == 60
    assert {row["group"] for row in preview["rows"]} == {"A", "B", "C"}


def test_cli_writes_a_package_once_and_refuses_overwrite(tmp_path):
    dataset = load_batch_dataset(DATASET)
    run = tmp_path / "run"
    run.mkdir()
    (run / "attempts.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in full_ledger(dataset)),
        encoding="utf-8")
    output = tmp_path / "package"
    assert main(["--run", str(run), "--dataset", str(DATASET), "--output", str(output)]) == 0
    for name in ("review.json", "key.json", "PACK_MANIFEST.json", "README.md"):
        assert (output / name).is_file(), name
    manifest = json.loads((output / "PACK_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["items"] == 20 and manifest["collection_mode"] == "real_model"
    assert set(manifest["files"]) == {"review.json", "key.json", "README.md"}
    assert main(["--run", str(run), "--dataset", str(DATASET), "--output", str(output)]) != 0
    assert main(["--run", str(run), "--dataset", str(DATASET), "--output", str(tmp_path / "stub"),
                 "--collection-mode", "stub"]) != 0
