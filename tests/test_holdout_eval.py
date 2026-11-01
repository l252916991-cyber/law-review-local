"""Six-case holdout: authored cases stay independent, frozen, and seeder-verifiable."""
from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from app.collaboration_ablation import (
    Generation,
    load_batch_dataset,
    load_prepared,
    prepare_batch,
    run_batch,
)
from scripts.build_holdout_eval import build, load_cases, main, validate_cross_case

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "benchmarks/answer_eval/holdout-v1"


def test_frozen_holdout_has_six_independent_cases_and_rebuilds_byte_identically():
    assert main([]) == 0
    dataset = load_batch_dataset(DIRECTORY / "dataset.json")
    assert len(dataset["cases"]) == 6
    assert len(dataset["items"]) == 24
    assert len(dataset["sources"]) == 24
    assert {item["case_id"] for item in dataset["items"]} == {1, 2, 3, 4, 5, 6}
    assert build(load_cases()) == dataset
    assert "未经律师或独立人工标注" in dataset["notice"]
    manifest = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_sha256"] == hashlib.sha256(
        (DIRECTORY / "dataset.json").read_bytes()).hexdigest()
    assert manifest["cases"] == 6 and manifest["pages"] == 24


def test_every_case_has_two_normals_and_one_tension_item():
    dataset = load_batch_dataset(DIRECTORY / "dataset.json")
    by_case: dict[int, list[str]] = {}
    for item in dataset["items"]:
        by_case.setdefault(item["case_id"], []).append(item["category"])
    for case_id, categories in by_case.items():
        assert categories.count("normal") == 2, case_id
        assert {"contradiction", "insufficient", "irrelevant", "statute_missing"} & set(categories), case_id
    counts = {category: sum(item["category"] == category for item in dataset["items"])
              for category in {item["category"] for item in dataset["items"]}}
    assert counts == {"normal": 12, "contradiction": 6, "insufficient": 3, "irrelevant": 2, "statute_missing": 1}


def test_question_cannot_cite_another_case_page():
    dataset = load_batch_dataset(DIRECTORY / "dataset.json")
    tampered = deepcopy(dataset)
    item = next(row for row in tampered["items"] if row["case_id"] == 1)
    other = next(source for source in tampered["sources"] if source["case_id"] == 2)
    item["case_id"] = 1
    item["acceptable_pages"] = [[other["document_name"], other["page"]]]
    item["must_include"] = [other["text"][:6]]
    with pytest.raises(ValueError, match="其他案例"):
        validate_cross_case(tampered)


def test_frozen_holdout_cannot_be_overwritten_after_a_question_change(tmp_path):
    target = tmp_path / "holdout"
    shutil.copytree(DIRECTORY, target)
    before = (target / "dataset.json").read_bytes()
    cases = json.loads((target / "cases.json").read_text(encoding="utf-8"))
    cases["cases"][0]["questions"][0]["question"] += "请补充依据。"
    (target / "cases.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="冻结产物不同"):
        main(["--directory", str(target), "--write"])
    assert (target / "dataset.json").read_bytes() == before


def test_prepare_seeds_each_holdout_case_and_keeps_contexts_inside_its_case(tmp_path):
    dataset = load_batch_dataset(DIRECTORY / "dataset.json")
    output = tmp_path / "holdout-batch"
    manifest = prepare_batch(dataset, output, item_ids=["A-normal-01", "F-normal-02"], limit=6)
    assert manifest["case"]["cases"] == [1, 2, 3, 4, 5, 6]
    assert manifest["case"]["pages"] == 24
    _, prepared = load_prepared(output)
    prefixes = {"A-normal-01": "A0", "F-normal-02": "F0"}
    for record in prepared:
        assert record["contexts"], record["id"]
        assert all(context["name"].startswith(prefixes[record["id"]]) for context in record["contexts"])
        assert record["case_id"] == (1 if record["id"].startswith("A") else 6)


def test_holdout_attempt_is_scored_by_its_own_case_item(tmp_path):
    dataset = load_batch_dataset(DIRECTORY / "dataset.json")
    output = tmp_path / "batch"
    prepare_batch(dataset, output, item_ids=["D-normal-01"], limit=6)
    _, prepared = load_prepared(output)
    answer = "借款本金500,000元，年利率12%[资料1]。请律师复核原文与上述结论。"
    report = run_batch(dataset, prepared, tmp_path / "run",
                       generate=lambda *args: Generation(text=answer, model="stub"), model="stub")
    rows = [json.loads(line) for line in
            (tmp_path / "run" / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
    row = next(entry for entry in rows if entry["group"] == "A")
    assert row["outcome"] == "ok" and row["passed"] is True
    assert row["score"]["coverage"] == 1.0
    assert report["summary"]["A"]["planned_attempts"] == 1


def test_prepare_rejects_an_unknown_item_id_instead_of_silently_dropping_it(tmp_path):
    dataset = load_batch_dataset(DIRECTORY / "dataset.json")
    with pytest.raises(ValueError, match="未知题目 id"):
        prepare_batch(dataset, tmp_path / "batch", item_ids=["A-normal-01", "C-insufficient-01"])
    assert not (tmp_path / "batch").exists()
