"""Corpus coverage must be measured from questions only and yield a usable worklist."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.legal_corpus import SCHEMA_VERSION, split_articles
from scripts.lawbench_coverage import coverage, load_rows, write_worklist


def _corpus(directory: Path) -> Path:
    directory.mkdir(parents=True)
    document = {
        "schema_version": SCHEMA_VERSION, "law_name": "中华人民共和国测试法",
        "aliases": ["中华人民共和国测试法", "测试法"],
        "version_date": "2020-01-01", "effective_date": None, "version_status": "historical",
        "source_url": "https://example.gov.cn/law", "document_id": "fixture",
        "articles": split_articles("第一条 测试内容。"),
    }
    raw = json.dumps(document, ensure_ascii=False).encode()
    (directory / "fixture.json").write_bytes(raw)
    (directory / "manifest.json").write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "documents": [{"document_file": "fixture.json", "document_sha256": hashlib.sha256(raw).hexdigest()}],
    }), encoding="utf-8")
    return directory


def _rows() -> list[dict[str, str]]:
    return [
        {"question_id": "1-1_0000", "task": "1-1", "task_name": "法条背诵", "instruction": "x",
         "question": "测试法第一条的内容是什么？"},
        {"question_id": "1-1_0001", "task": "1-1", "task_name": "法条背诵", "instruction": "x",
         "question": "其他法第一条的内容是什么？"},
    ]


def test_coverage_reports_hits_and_missing_laws(tmp_path):
    result = coverage(_rows(), [str(_corpus(tmp_path / "corpus"))], ["1-1"])
    assert result["tasks"]["1-1"]["coverage"] == 0.5
    assert result["tasks"]["1-1"]["modes"] == {"exact_article": 1, "law_not_found": 1}
    assert [entry["law_name"] for entry in result["missing_laws"]] == ["中华人民共和国其他法"]
    assert result["missing_question_count"] == 1
    assert result["worklist"][0]["question_id"] == "1-1_0001"


def test_worklist_is_campaign_compatible(tmp_path):
    result = coverage(_rows(), [str(_corpus(tmp_path / "corpus"))], ["1-1"])
    path = tmp_path / "worklist" / "inputs.jsonl"
    write_worklist(result["worklist"], path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"question_id": "1-1_0001", "task": "1-1", "task_name": "法条背诵", "instruction": "x",
                     "question": "其他法第一条的内容是什么？"}]


def test_load_rows_filters_tasks_and_rejects_unsupported(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "detailed_results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in _rows()), encoding="utf-8",
    )
    assert [row["question_id"] for row in load_rows(run, ["1-1"])] == ["1-1_0000", "1-1_0001"]
    with pytest.raises(ValueError, match="does not support"):
        load_rows(run, ["9-9"])
