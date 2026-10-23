"""The citation boundary: an explicit reference either verifies exactly or fails closed.

The regression cases below are the ones that previously returned ``ok`` while citing
the wrong statute or the wrong article. ``ok`` must be reachable only through an exact
corpus lookup; text search may never promote a nearby passage to a citation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.legal_corpus import SCHEMA_VERSION, split_articles
from app.statutory_retrieval import corpus_status, retrieve_statutory


def _corpus(directory: Path) -> str:
    directory.mkdir(parents=True)
    entries = []
    documents = [
        # 百/千位条号: the product parser used to truncate 264 -> 14 and 1060 -> 10.
        ("中华人民共和国刑法", "刑法", "第二百六十四条 盗窃公私财物。\n第一百三十三条 交通肇事。"),
        ("中华人民共和国民法典", "民法典", "第五百零九条 全面履行。\n第一千零六十条 日常家事代理。"),
        # Two distinct statutes sharing a bare alias must not auto-resolve.
        ("甲测试法", "重名法", "第一条 甲内容。"),
        ("乙测试法", "重名法", "第一条 乙内容。"),
    ]
    for index, (law_name, alias, text) in enumerate(documents, start=1):
        document = {
            "schema_version": SCHEMA_VERSION, "law_name": law_name, "aliases": [law_name, alias],
            "version_date": "2020-01-01", "effective_date": None, "version_status": "fixture",
            "source_url": "https://example.gov.cn/law", "document_id": f"fixture-{index}",
            "articles": split_articles(text),
        }
        name = f"fixture-{index}.json"
        raw = json.dumps(document, ensure_ascii=False).encode()
        (directory / name).write_bytes(raw)
        entries.append({"document_file": name, "document_sha256": hashlib.sha256(raw).hexdigest()})
    (directory / "manifest.json").write_text(json.dumps(
        {"schema_version": SCHEMA_VERSION, "documents": entries}), encoding="utf-8")
    return str(directory)


@pytest.fixture
def corpus_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAW_REVIEW_LEGAL_CORPUS_DIR", _corpus(tmp_path / "corpus"))


@pytest.mark.parametrize("question,law,article", [
    ("刑法第二百六十四条的内容是什么？", "中华人民共和国刑法", 264),
    ("刑法第一百三十三条的内容是什么？", "中华人民共和国刑法", 133),
    ("民法典第一千零六十条的内容是什么？", "中华人民共和国民法典", 1060),
    ("民法典第五百零九条的内容是什么？", "中华人民共和国民法典", 509),
])
def test_explicit_reference_verifies_to_the_named_law_and_article(corpus_env, question, law, article):
    result = retrieve_statutory(question)
    assert result["status"] == "ok" and result["mode"] == "citation"
    assert result["hits"][0]["law_name"] == law
    assert result["hits"][0]["article_number"] == article


@pytest.mark.parametrize("question", [
    "工伤保险条例第十四条怎么规定",       # named law absent from the corpus
    "不存在法第三十条是什么",
    "民法典第N条",                        # unusable article number, no citation
    "重名法第一条是什么",                  # one alias resolving to two statutes
    "刑法第八百条是什么",                  # article does not exist
])
def test_unresolvable_references_never_return_ok(corpus_env, question):
    result = retrieve_statutory(question)
    assert result["status"] in {"needs_review", "candidate"}
    assert result["status"] != "ok"
    for hit in result["hits"]:
        # A nearby statute must never be reported in place of the named one.
        assert hit["law_name"] not in {"中华人民共和国刑法", "中华人民共和国民法典"}


def test_text_search_is_discovery_and_never_a_citation(corpus_env):
    result = retrieve_statutory("全面履行 日常家事代理")
    assert result["status"] == "candidate" and result["mode"] == "discovery"
    assert result["hits"]


def test_missing_corpus_fails_closed(monkeypatch):
    monkeypatch.delenv("LAW_REVIEW_LEGAL_CORPUS_DIR", raising=False)
    result = retrieve_statutory("刑法第二百六十四条")
    assert result["status"] == "needs_review" and result["hits"] == []
    status = corpus_status()
    assert status["configured"] is False and status["available"] is False


def test_corpus_status_reports_broken_integrity(tmp_path, monkeypatch):
    directory = Path(_corpus(tmp_path / "corpus"))
    (directory / "fixture-1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LAW_REVIEW_LEGAL_CORPUS_DIR", str(directory))
    status = corpus_status()
    assert status["configured"] is True and status["available"] is False
    assert "integrity" in status["error"].lower() or "Mismatch" in status["error"]
    assert retrieve_statutory("刑法第二百六十四条")["status"] == "needs_review"
