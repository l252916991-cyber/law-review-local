from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.corpus_audit import audit, classify, dedup_digest, heading_leak_articles
from app.legal_corpus import SCHEMA_VERSION, split_articles
from scripts.audit_corpus_conflicts import main as audit_main


def write_corpus(root: Path, name: str, laws: list[tuple[str, str, str]]) -> str:
    """laws: (law_name, version_date, article_text); writes one document per law."""
    entries = []
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    for index, (law, version, text) in enumerate(laws):
        document_id = f"{name}-{index}"
        doc = {"schema_version": SCHEMA_VERSION, "document_id": document_id, "law_name": law,
               "aliases": [law], "version_date": version, "effective_date": None,
               "version_status": "test_fixture", "source_url": f"https://example.invalid/{document_id}",
               "articles": split_articles(text)}
        raw = json.dumps(doc, ensure_ascii=False).encode()
        (directory / f"{document_id}.json").write_bytes(raw)
        entries.append({"document_file": f"{document_id}.json", "document_sha256": hashlib.sha256(raw).hexdigest()})
    (directory / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    return str(directory)


def write_raw_corpus(root: Path, name: str, law: str, version: str, articles: list[tuple[str, str]]) -> str:
    """Write explicit article bodies, bypassing split_articles, to reproduce parser leaks."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    document_id = f"{name}-0"
    doc = {"schema_version": SCHEMA_VERSION, "document_id": document_id, "law_name": law,
           "aliases": [law], "version_date": version, "effective_date": None,
           "version_status": "test_fixture", "source_url": f"https://example.invalid/{document_id}",
           "articles": [{"article_id": aid, "text": text} for aid, text in articles]}
    raw = json.dumps(doc, ensure_ascii=False).encode()
    (directory / f"{document_id}.json").write_bytes(raw)
    (directory / "manifest.json").write_text(json.dumps(
        {"schema_version": SCHEMA_VERSION,
         "documents": [{"document_file": f"{document_id}.json", "document_sha256": hashlib.sha256(raw).hexdigest()}]}))
    return str(directory)


def test_exact_cosmetic_and_substantive(tmp_path):
    a = write_corpus(tmp_path, "a", [("测试法", "2020-01-01", "第一条 内容一致。\n第二条 另起一行。")])
    exact = write_corpus(tmp_path, "exact", [("测试法", "2020-01-01", "第一条 内容一致。\n第二条 另起一行。")])
    cosmetic = write_corpus(tmp_path, "cosmetic", [("测试法", "2020-01-01", "第一条 【目的】内容一致,\n第二条 另起一行。")])
    real = write_corpus(tmp_path, "real", [("测试法", "2020-01-01", "第一条 完全不同的内容。\n第二条 另起一行。")])
    assert audit([a, exact])["conflicts"][0]["difference_type"] == "exact_duplicate"
    assert audit([a, cosmetic])["conflicts"][0]["difference_type"] == "cosmetic_duplicate"
    assert audit([a, real])["conflicts"][0]["difference_type"] == "substantive_conflict"


def test_boundary_difference_isolates_heading_leak(tmp_path):
    # A heading line glued onto the previous article body is a builder boundary bug,
    # not a formatting difference and not a real text conflict (the民法典 附则 case).
    clean = write_raw_corpus(tmp_path, "clean", "测试法", "2020-01-01", [("1", "第一条 甲。"), ("2", "第二条 乙。")])
    leaky = write_raw_corpus(tmp_path, "leaky", "测试法", "2020-01-01",
                             [("1", "第一条 甲。"), ("2", "第二条 乙。\n附 则")])
    record = audit([clean, leaky])["conflicts"][0]
    assert record["difference_type"] == "boundary_difference"
    assert record["different_article_ids"] == ["2"]
    assert "附则" in record["heading_difference"]
    assert record["semantic_normalized_hash_equal"] is False
    assert record["recommended_action"].startswith("FIX_BUILDER")


def test_three_identical_copies_collapse_to_one_pair(tmp_path):
    text = "第一条 同文。"
    dirs = [write_corpus(tmp_path, name, [("测试法", "2020-01-01", text)]) for name in ("a", "b", "c")]
    report = audit(dirs)
    assert report["duplicate_keys"] == 1 and report["pair_count"] == 1
    record = report["conflicts"][0]
    assert record["difference_type"] == "exact_duplicate" and len(record["all_source_dirs"]) == 3


def test_distinct_laws_and_versions_are_not_paired(tmp_path):
    a = write_corpus(tmp_path, "a", [("甲法", "2020-01-01", "第一条 甲。"), ("乙法", "2020-01-01", "第一条 乙。")])
    b = write_corpus(tmp_path, "b", [("甲法", "2021-01-01", "第一条 甲修。")])
    report = audit([a, b])
    assert report["pair_count"] == 0 and report["classification_counts"]["substantive_conflict"] == 0


def test_projection_helpers_are_shared_with_builder():
    # dedup projection ignores caption/punctuation/whitespace differences.
    assert dedup_digest({"1": "第一条 【目的】内容,"}) == dedup_digest({"1": "第一条 内容。"})
    assert dedup_digest({"1": "甲"}) != dedup_digest({"1": "乙"})
    assert heading_leak_articles({"1": "第一条 甲。", "2": "第二条 乙。\n附 则"}) == ["2"]
    assert heading_leak_articles({"1": "第一条 甲。"}) == []


def test_classify_is_pure_on_prepared_publications():
    a = {"source_dir": "x", "document_id": "d", "source_url": None, "article_count": 1,
         "manifest_document_sha256": "a", "manifest_raw_sha256": "b", "articles": {"1": "第一条 甲。"}}
    b = {"source_dir": "y", "document_id": "e", "source_url": None, "article_count": 1,
         "manifest_document_sha256": "c", "manifest_raw_sha256": "d", "articles": {"1": "第一条 甲。"}}
    assert classify(a, b)["difference_type"] == "exact_duplicate"
    assert classify(a, b)["dedup_sha256_a"] == classify(a, b)["dedup_sha256_b"]


def test_real_frozen_corpus_is_classified_cleanly(tmp_path):
    """The checked-in corpus is our regression input when present; skip offline."""
    root = Path(__file__).resolve().parents[1] / "output/score85"
    directories = sorted(str(path) for path in root.glob("legal_corpus*") if (path / "manifest.json").exists())
    if len(directories) < 2:
        import pytest

        pytest.skip("frozen statute corpus not checked out")
    report = audit(directories)
    assert report["classification_counts"]["substantive_conflict"] == 0
    assert report["classification_counts"]["boundary_difference"] == 1  # 民法典 附则 leak
    for record in report["conflicts"]:
        assert record["recommended_action"]
    out = tmp_path / "report.json"
    assert audit_main(["--corpus-dir", directories[0], "--corpus-dir", directories[1], "--output", str(out)]) == 0
    assert json.loads(out.read_text())["schema_version"] == "corpus-conflict-audit-v1"
