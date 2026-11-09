"""Frozen answer-set provenance, independent source grounding and strict citations."""
import json
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest

from app.evaluation.answer_eval import load_dataset, score_answer, validate_dataset
from scripts.build_answer_eval import build, main

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "benchmarks/answer_eval/v2"


def test_frozen_sixty_items_and_all_sources_reproduce_from_seed():
    payload = load_dataset(DIRECTORY / "dataset.json")
    assert len(payload["items"]) == 60
    assert len(payload["sources"]) == 14
    assert build(json.loads((DIRECTORY / "questions.json").read_text())) == payload
    assert main([]) == 0
    assert "assistant_generated" in payload["annotation_source"]
    assert "未经律师或独立人工标注" in payload["notice"]


def test_frozen_version_cannot_be_overwritten_after_question_change():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "v2"
        shutil.copytree(DIRECTORY, target)
        before = (target / "dataset.json").read_bytes()
        questions = json.loads((target / "questions.json").read_text())
        questions[0]["question"] += "请核对。"
        (target / "questions.json").write_text(json.dumps(questions, ensure_ascii=False))
        with pytest.raises(ValueError, match="冻结产物不同"):
            main(["--directory", str(target), "--write"])
        assert (target / "dataset.json").read_bytes() == before


@pytest.mark.parametrize("mutation", ["duplicate_id", "missing_page", "unsupported_fact", "wrong_hash", "bool_page"])
def test_invalid_grounding_is_rejected(mutation):
    payload = deepcopy(load_dataset(DIRECTORY / "dataset.json"))
    if mutation == "duplicate_id": payload["items"][1]["id"] = payload["items"][0]["id"]
    elif mutation == "missing_page": payload["items"][0]["acceptable_pages"] = [["不存在.txt", 1]]
    elif mutation == "unsupported_fact": payload["items"][0]["must_include"] = ["不存在的要点"]
    elif mutation == "wrong_hash": payload["sources"][0]["text"] += "篡改"
    elif mutation == "bool_page": payload["items"][0]["acceptable_pages"] = [["01_起诉意见书.txt", True]]
    with pytest.raises(ValueError):
        validate_dataset(payload)


def test_v2_answer_without_citations_cannot_pass_on_keywords_alone():
    item = load_dataset(DIRECTORY / "dataset.json")["items"][0]
    answer = "累计流入2,860万元，涉及47名投资人。"
    assert not score_answer(item, answer, citations=[])["passed"]
    assert not score_answer(item, answer, citations=[{"document_name": "04_账户流水摘要.txt", "page": True}])["passed"]
    assert score_answer(item, answer, citations=[{"document_name": "01_起诉意见书.txt", "page": 2}])["passed"]
