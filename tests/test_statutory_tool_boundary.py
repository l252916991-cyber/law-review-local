"""Fail-closed boundary of the structured statute tools and of version selection.

``retrieve_statutory`` parses a question; ``verify_citation`` and
``search_statute_text`` are the tool-call surfaces. All three must keep the same
rule: ``ok`` only for one statute and one resolvable article; ambiguity, a missing
corpus or a broken corpus is ``needs_review``; text search is discovery only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app import statutory_retrieval
from app.legal_corpus import SCHEMA_VERSION, LegalCorpus, split_articles
from app.statutory_retrieval import (
    corpus_status,
    parse_version,
    retrieve_statutory,
    search_statute_text,
    verify_citation,
)

DOCUMENTS = [
    # (law_name, alias, version_date, text)
    ("中华人民共和国刑法", "刑法", "2020-12-26", "第二百六十四条 盗窃公私财物。\n第一百三十三条 交通肇事。"),
    ("甲测试法", "重名法", "2020-01-01", "第一条 甲内容。"),
    ("乙测试法", "重名法", "2020-01-01", "第一条 乙内容。"),
    # One statute published in two versions: the caller must pick one.
    ("中华人民共和国版本测试法", "版本测试法", "2018-10-26", "第一条 旧版内容。"),
    ("中华人民共和国版本测试法", "版本测试法", "2023-12-29", "第一条 新版内容。"),
]


def build_corpus(directory: Path, documents=DOCUMENTS) -> Path:
    directory.mkdir(parents=True)
    entries = []
    for index, (law_name, alias, version_date, text) in enumerate(documents, start=1):
        document = {
            "schema_version": SCHEMA_VERSION, "law_name": law_name, "aliases": [law_name, alias],
            "version_date": version_date, "effective_date": None, "version_status": "fixture",
            "source_url": "https://example.gov.cn/law", "document_id": f"fixture-{index}",
            "articles": split_articles(text),
        }
        name = f"fixture-{index}.json"
        raw = json.dumps(document, ensure_ascii=False).encode()
        (directory / name).write_bytes(raw)
        entries.append({"document_file": name, "document_sha256": hashlib.sha256(raw).hexdigest()})
    (directory / "manifest.json").write_text(json.dumps(
        {"schema_version": SCHEMA_VERSION, "documents": entries}), encoding="utf-8")
    return directory


@pytest.fixture
def corpus_dir(tmp_path, monkeypatch):
    directory = build_corpus(tmp_path / "corpus")
    monkeypatch.setenv("LAW_REVIEW_LEGAL_CORPUS_DIR", str(directory))
    return directory


def assert_review(result: dict) -> None:
    assert result["status"] == "needs_review"
    assert result["hits"] == []
    assert result["warning"]


# --- corpus loading and readiness -------------------------------------------------

def test_status_reports_available_corpus_and_loads_it_once(corpus_dir):
    status = corpus_status()
    assert status == {"configured": True, "available": True, "directory": str(corpus_dir),
                      "documents": len(DOCUMENTS), "error": None}
    first = statutory_retrieval._configured_corpus()
    assert statutory_retrieval._configured_corpus() is first


def test_a_different_directory_is_loaded_instead_of_the_cached_one(tmp_path, monkeypatch, corpus_dir):
    statutory_retrieval._configured_corpus()
    other = build_corpus(tmp_path / "other", DOCUMENTS[:1])
    monkeypatch.setenv("LAW_REVIEW_LEGAL_CORPUS_DIR", str(other))
    assert corpus_status()["documents"] == 1


# --- version selection --------------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("版本测试法第一条（2018年10月26日版本）", "2018-10-26"),
    ("按 2023-12-29 公布的版本", "2023-12-29"),
    ("没有日期的问题", None),
])
def test_parse_version_normalises_explicit_dates(question, expected):
    assert parse_version(question) == expected


def test_unversioned_reference_to_a_multi_version_statute_is_refused(corpus_dir):
    result = retrieve_statutory("版本测试法第一条是什么？")
    assert_review(result)
    assert "指定适用版本" in result["summary"]


def test_explicit_version_selects_exactly_that_text(corpus_dir):
    result = retrieve_statutory("版本测试法第一条（2018年10月26日版本）")
    assert result["status"] == "ok"
    assert [(hit["version_date"], hit["text"]) for hit in result["hits"]] == [("2018-10-26", "第一条 旧版内容。")]


def test_lookup_errors_inside_retrieval_fail_closed():
    class BrokenCorpus:
        def lookup(self, *args, **kwargs):
            raise OSError("disk read failed")

    result = retrieve_statutory("刑法第二百六十四条", corpus=BrokenCorpus())
    assert_review(result)
    assert result["warning"] == "OSError"


# --- verify_citation: the structured tool surface ------------------------------------

def test_verify_citation_returns_the_single_named_article(corpus_dir):
    result = verify_citation("刑法", 264)
    assert result["status"] == "ok" and result["mode"] == "citation"
    assert [(hit["law_name"], hit["article_number"]) for hit in result["hits"]] == [("中华人民共和国刑法", 264)]
    assert set(result["hits"][0]) == set(statutory_retrieval.CITATION_FIELDS)


@pytest.mark.parametrize("law_name,article_number", [
    ("", 264),
    ("   ", 264),
    (None, 264),
    ("刑法", 0),
    ("刑法", -1),
    ("刑法", "264"),
    ("刑法", True),        # bool is an int subclass but is not an article number
    ("刑法", 264.0),
])
def test_verify_citation_rejects_malformed_input_before_touching_the_corpus(monkeypatch, law_name, article_number):
    def unreachable():
        raise AssertionError("corpus must not be loaded for malformed input")

    monkeypatch.setattr(statutory_retrieval, "_configured_corpus", unreachable)
    assert_review(verify_citation(law_name, article_number))


@pytest.mark.parametrize("law_name,article_number", [
    ("工伤保险条例", 14),   # statute absent from the corpus
    ("刑法", 800),          # article absent
    ("重名法", 1),          # one alias, two statutes
])
def test_verify_citation_never_substitutes_a_nearby_article(corpus_dir, law_name, article_number):
    result = verify_citation(law_name, article_number)
    assert_review(result)
    assert law_name in result["summary"]


def test_verify_citation_requires_a_version_when_several_exist(corpus_dir):
    assert "指定适用版本" in verify_citation("版本测试法", 1)["summary"]
    chosen = verify_citation("版本测试法", 1, "2023-12-29")
    assert chosen["status"] == "ok"
    assert [hit["text"] for hit in chosen["hits"]] == ["第一条 新版内容。"]


def test_verify_citation_without_corpus_or_with_a_broken_one_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("LAW_REVIEW_LEGAL_CORPUS_DIR", raising=False)
    assert "未配置" in verify_citation("刑法", 264)["summary"]

    broken = build_corpus(tmp_path / "broken")
    (broken / "fixture-1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LAW_REVIEW_LEGAL_CORPUS_DIR", str(broken))
    result = verify_citation("刑法", 264)
    assert_review(result)
    assert result["warning"] == "ValueError"


def test_verify_citation_lookup_errors_fail_closed():
    class BrokenCorpus:
        def lookup(self, *args, **kwargs):
            raise KeyError("articles")

    result = verify_citation("刑法", 264, corpus=BrokenCorpus())
    assert_review(result)
    assert result["warning"] == "KeyError"


# --- search_statute_text: discovery only ---------------------------------------------

def test_search_is_candidate_only_and_carries_version_and_source(corpus_dir):
    result = search_statute_text("盗窃公私财物")
    assert result["status"] == "candidate" and result["mode"] == "discovery"
    assert result["hits"][0]["law_name"] == "中华人民共和国刑法"
    assert result["hits"][0]["version_date"] == "2020-12-26"
    assert result["hits"][0]["source_url"]


def test_search_without_a_match_asks_for_an_explicit_citation(corpus_dir):
    result = search_statute_text("量子香蕉")
    assert_review(result)
    assert "法名与条号" in result["warning"]


@pytest.mark.parametrize("query", ["", "   ", "法" * 501, None])
def test_search_rejects_empty_or_oversized_queries(query):
    assert_review(search_statute_text(query))


def test_search_without_corpus_or_on_search_failure_fails_closed(monkeypatch):
    monkeypatch.delenv("LAW_REVIEW_LEGAL_CORPUS_DIR", raising=False)
    assert "未配置" in search_statute_text("盗窃")["summary"]

    class BrokenCorpus:
        def search(self, *args, **kwargs):
            raise OSError("index unreadable")

    monkeypatch.setattr(statutory_retrieval, "_configured_corpus", lambda: BrokenCorpus())
    result = search_statute_text("盗窃")
    assert_review(result)
    assert result["warning"] == "OSError"


def test_search_on_unreadable_corpus_fails_closed(monkeypatch):
    def unreadable():
        raise OSError("permission denied")

    monkeypatch.setattr(statutory_retrieval, "_configured_corpus", unreadable)
    result = search_statute_text("盗窃")
    assert_review(result)
    assert result["warning"] == "OSError"


def test_the_fixture_corpus_is_a_real_legal_corpus(corpus_dir):
    # Guard: the tests above exercise the production loader, not a stand-in.
    assert len(LegalCorpus(corpus_dir).documents) == len(DOCUMENTS)
