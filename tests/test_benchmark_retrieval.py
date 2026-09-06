import hashlib
import json

import pytest

from app.benchmark_retrieval import corpus_fingerprint, retrieve
from app.legal_corpus import SCHEMA_VERSION, split_articles


@pytest.fixture
def corpus(tmp_path):
    entries = []
    for year, text in [(2006, "第一条 历史成员资格。\n第二条 保留历史规定。"),
                       (2017, "第一条 修订成员资格。\n第二条 修订规定。\n第二条之一 补充成员资格。")]:
        doc = {"schema_version": SCHEMA_VERSION, "law_name": "测试法", "aliases": ["测试法"],
               "version_date": f"{year}-01-01", "effective_date": None, "version_status": "test_fixture",
               "source_url": "https://example.gov.cn/law", "document_id": f"fixture-{year}",
               "articles": split_articles(text)}
        name = f"{year}.json"
        raw = json.dumps(doc, ensure_ascii=False).encode()
        (tmp_path / name).write_bytes(raw)
        entries.append({"document_file": name, "document_sha256": hashlib.sha256(raw).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    return [str(tmp_path)]


def test_exact_version_policy_and_subarticle(corpus):
    current = retrieve("1-1", "测试法第一条是什么？", corpus)
    assert current["hits"][0]["version_date"] == "2017-01-01"
    old = retrieve("1-1", "测试法2006年第一条是什么？", corpus)
    assert old["hits"][0]["text"] == "第一条 历史成员资格。"
    sub = retrieve("1-1", "测试法第二条之一是什么？", corpus)
    assert sub["hits"][0]["article_id"] == "2-1"


@pytest.mark.parametrize("question,mode", [
    ("不存在法第一条是什么？", "law_not_found"),
    ("测试法2008年第一条是什么？", "exact_article"),
    ("测试法第八百条是什么？", "exact_article"),
    ("测试法2006年与2017年第一条是什么？", "version_ambiguous"),
])
def test_no_fabricated_or_wrong_version_fallback(corpus, question, mode):
    result = retrieve("1-1", question, corpus)
    assert result["hits"] == [] and result["context"] == ""
    assert result["mode"] == mode


def test_search_selects_one_version_per_law_and_ignores_case_year(corpus):
    result = retrieve("3-2", "2006年成员资格争议涉及测试法。", corpus)
    assert result["hits"]
    assert {hit["version_date"] for hit in result["hits"]} == {"2017-01-01"}
    assert all(hit["text"] in result["context"] and hit["source_url"] in result["context"] for hit in result["hits"])
    assert retrieve("2-1", "成员资格", ["/does-not-exist"])["mode"] == "skipped"


def test_fingerprint_rejects_changes_and_duplicate_sources_deduplicate(corpus):
    hashes = corpus_fingerprint(corpus)
    assert len(hashes) == 3
    assert retrieve("1-1", "测试法第一条", corpus * 2)["hits"] == retrieve("1-1", "测试法第一条", corpus)["hits"]
    from pathlib import Path
    Path(corpus[0], "2006.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        corpus_fingerprint(corpus)


def test_budget_limits_reject_invalid_values(corpus):
    with pytest.raises(ValueError, match="budget"):
        retrieve("1-1", "测试法第一条", corpus, limit=0)
