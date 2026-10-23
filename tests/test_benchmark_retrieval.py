import hashlib
import json

import pytest

from app.benchmark_retrieval import corpus_fingerprint, matched_law_names, retrieve
from app.legal_corpus import SCHEMA_VERSION, split_articles


def _law(name, aliases):
    return {"law_name": name, "aliases": aliases}


COMPANY = _law("中华人民共和国公司法", ["中华人民共和国公司法", "公司法"])
LABOR = _law("中华人民共和国劳动法", ["中华人民共和国劳动法", "劳动法"])


@pytest.mark.parametrize("question,expected", [
    # Substring collisions: 公司法 inside 公司法定代表人 / 公司法人 are not references.
    ("其担任公司法定代表人。", set()),
    ("该公司法人刘某借款", set()),
    ("一人有限责任公司法定代表人以公司法人名义贷款", set()),
    # Genuine references must survive.
    ("根据公司法第二百条规定", {"中华人民共和国公司法"}),
    ("依照公司法规定应当承担", {"中华人民共和国公司法"}),
    ("违反劳动法规怎么办", {"中华人民共和国劳动法"}),
    ("按照劳动法的一半进行赔偿", {"中华人民共和国劳动法"}),
    # A collision in one place does not erase a real reference elsewhere.
    ("公司法定代表人和公司法第二百条", {"中华人民共和国公司法"}),
])
def test_matched_law_names_rejects_embedded_substrings(question, expected):
    assert matched_law_names(question, [COMPANY, LABOR]) == expected


def test_matched_law_names_prefers_longest_alias():
    # 道路交通安全法 is a substring of its own implementing regulation; the longer
    # name must win so the specific statute is not read as the general one.
    docs = [_law("中华人民共和国道路交通安全法",
                 ["中华人民共和国道路交通安全法", "道路交通安全法"]),
            _law("中华人民共和国道路交通安全法实施条例",
                 ["中华人民共和国道路交通安全法实施条例", "道路交通安全法实施条例"])]
    assert matched_law_names("根据道路交通安全法实施条例第二十四条", docs) == {"中华人民共和国道路交通安全法实施条例"}
    assert matched_law_names("根据道路交通安全法第七十条", docs) == {"中华人民共和国道路交通安全法"}


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


def test_amendment_falls_back_to_consolidated_base_statute(tmp_path):
    entries = []
    fixtures = [
        ("中华人民共和国宪法修正案（2018年）", ["宪法修正案2018年"], "（被修正文条）第三十二条 修正内容。"),
        ("中华人民共和国宪法（2018年修正文本）", ["中华人民共和国宪法", "宪法"], "第一条 主权。\n第一百二十六条 监察委员会对人大负责。"),
    ]
    for law_name, extra_aliases, text in fixtures:
        doc = {"schema_version": SCHEMA_VERSION, "law_name": law_name,
               "aliases": sorted({law_name, law_name.removeprefix("中华人民共和国"), *extra_aliases}),
               "version_date": "2018-03-11", "effective_date": None, "version_status": "test_fixture",
               "source_url": "https://example.gov.cn/law", "document_id": law_name,
               "articles": split_articles(text)}
        name = f"{law_name}.json"
        raw = json.dumps(doc, ensure_ascii=False).encode()
        (tmp_path / name).write_bytes(raw)
        entries.append({"document_file": name, "document_sha256": hashlib.sha256(raw).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    result = retrieve("1-1", "宪法修正案2018年第一百二十六条的内容是什么？", [str(tmp_path)])
    assert result["hits"][0]["article_id"] == "126"
    assert "监察委员会对人大负责" in result["hits"][0]["text"]
