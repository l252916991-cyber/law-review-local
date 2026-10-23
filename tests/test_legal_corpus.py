from __future__ import annotations

import gzip
import hashlib
import json

import pytest

from app.legal_corpus import LegalCorpus, SCHEMA_VERSION, VersionAmbiguityError, article_number, html_to_text, split_articles
import scripts.build_legal_corpus as corpus_builder
from scripts.build_legal_corpus import build_document


def test_ministry_supplement_sources_are_complete_official_publications():
    expected = {
        "traffic-points-2021": ("www.gov.cn", 37),
        "medical-device-registration-2021": ("www.samr.gov.cn", 124),
        "medical-device-operation-2022": ("www.samr.gov.cn", 73),
        "cosmetics-operation-2021": ("www.samr.gov.cn", 66),
    }
    selected = {source["document_id"]: source for source in corpus_builder.SOURCES if source["document_id"] in expected}

    assert set(selected) == set(expected)
    assert len({source["document_id"] for source in corpus_builder.SOURCES}) == len(corpus_builder.SOURCES)
    for document_id, (host, expected_max) in expected.items():
        source = selected[document_id]
        assert source["source_url"].startswith(f"https://{host}/")
        assert source["expected_max"] == expected_max
        assert source["version_status"] == "dated_original_publication; currentness_not_asserted"
        assert source["version_evidence"]
        assert source["last_sentence"]


def test_statutory_paragraphs_references_and_subarticles():
    text = html_to_text("<script>第一条 fake</script><p>第一条 内容。</p><p>依照本法第二条处理。</p>"
                        "<p>第一条之一 补充。</p><h3>第二章 其他</h3><p>第二条 结束。</p>")
    articles = split_articles(text, expected_max=2)
    assert [item["article_id"] for item in articles] == ["1", "1-1", "2"]
    assert articles[0]["text"] == "第一条 内容。\n依照本法第二条处理。"
    assert "第二章" not in articles[1]["text"]
    assert articles[2]["section_heading"] == "第二章 其他"
    assert article_number("一千二百六十") == 1260
    assert article_number("一百零三") == 103


def test_incomplete_or_duplicate_publication_rejected():
    with pytest.raises(ValueError, match="Incomplete"):
        split_articles("第一条 内容。\n第三条 内容。", expected_max=3)
    with pytest.raises(ValueError, match="Duplicate"):
        split_articles("第一条 内容。\n第一条 内容。")


def test_version_evidence_and_footer_boundary():
    source = {"document_id": "fixture", "law_name": "测试法", "source_url": "https://example.gov.cn/law",
              "version_date": "2020-01-01", "effective_date": None, "version_status": "historical",
              "version_evidence": "2020年1月1日通过", "last_sentence": "施行。", "expected_max": 2}
    raw = "<p>2020年1月1日通过</p><p>第一条 内容。</p><p>第二条 自2020年2月1日起施行。</p><div>网站导航</div>".encode()
    doc = build_document(source, raw, "2026-09-05T00:00:00Z")
    assert doc["document_year"] == 2020
    assert doc["effective_date"] is None
    assert doc["articles"][-1]["text"].endswith("施行。")
    assert "网站导航" not in doc["articles"][-1]["text"]
    assert doc["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="Declared version"):
        build_document({**source, "version_evidence": "2030年1月1日通过"}, raw, "today")


def test_fullwidth_version_evidence_and_multiline_final_article():
    source = {"document_id": "fixture", "source_url": "https://example.gov.cn/law", "version_date": "2020-01-01",
              "version_evidence": "2020年1月1日通过", "last_sentence": "继续适用。", "expected_max": 1,
              "appendix_last_text": "最后一项规定"}
    raw = "<p>２０２０年１月１日通过</p><p>第一条 明日起施行。</p><p>既有规定继续适用。</p><p>附件一</p><p>最后一项规定</p><p>网站页脚</p>".encode()
    doc = build_document(source, raw, "today")
    assert doc["articles"][0]["text"] == "第一条 明日起施行。\n既有规定继续适用。"
    assert doc["appendix_text"] == "附件一\n最后一项规定"


def test_retrieval_requires_version_and_detects_tampering(tmp_path):
    entries = []
    for year, body in [(2006, "第一条 历史成员资格。"), (2017, "第一条 修订后的成员资格。")]:
        doc = {"schema_version": SCHEMA_VERSION, "law_name": "测试法", "aliases": ["测试法"],
               "version_date": f"{year}-01-01", "effective_date": None, "version_status": "historical",
               "source_url": "https://example.gov.cn/law", "document_id": f"fixture-{year}",
               "articles": split_articles(body)}
        raw = json.dumps(doc, ensure_ascii=False).encode()
        name = f"{year}.json"
        (tmp_path / name).write_bytes(raw)
        entries.append({"document_file": name, "document_sha256": hashlib.sha256(raw).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    corpus = LegalCorpus(tmp_path)
    with pytest.raises(VersionAmbiguityError):
        corpus.lookup("测试法", 1)
    with pytest.raises(VersionAmbiguityError):
        corpus.search("成员", law_name="测试法")
    result = corpus.lookup("测试法", 1, version_date="2006-01-01")
    assert result[0]["text"] == "第一条 历史成员资格。"
    assert corpus.lookup("测试法", 1, version_date="2010-01-01") == []
    assert corpus.search("成员资格", law_name="测试法", version_date="2017-01-01")[0]["version_date"] == "2017-01-01"
    (tmp_path / "2006.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity mismatch"):
        LegalCorpus(tmp_path)


def test_amendment_sequence_can_start_above_one():
    source = {"document_id": "amendment", "source_url": "https://example.gov.cn/law", "version_date": "2004-03-14",
              "version_evidence": "2004年3月14日", "last_sentence": "结束。", "expected_min": 18, "expected_max": 19}
    raw = "<p>2004年3月14日</p><p>第十八条 开始。</p><p>第十九条 结束。</p>".encode()
    doc = build_document(source, raw, "today")
    assert [article["article_id"] for article in doc["articles"]] == ["18", "19"]
    with pytest.raises(ValueError, match="Incomplete amendment"):
        build_document(source, raw.replace("第十八条".encode(), "第十七条".encode()), "today")


def test_builder_decompresses_gzip_publication_before_freezing(tmp_path, monkeypatch):
    source = {
        "document_id": "gzip-fixture", "law_name": "测试法", "aliases": ["测试法"],
        "source_url": "https://example.gov.cn/law", "publisher": "测试机关",
        "version_date": "2020-01-01", "effective_date": None,
        "version_status": "historical", "expected_max": 1,
        "version_evidence": "2020年1月1日", "last_sentence": "施行。",
    }
    publication = "<p>2020年1月1日通过</p><p>第一条 本法施行。</p>".encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return source["source_url"]

        def read(self, _limit):
            return gzip.compress(publication)

    monkeypatch.setattr(corpus_builder, "SOURCES", [source])
    monkeypatch.setattr(corpus_builder, "urlopen", lambda *_args, **_kwargs: Response())
    manifest = corpus_builder.build(tmp_path, {source["document_id"]})

    assert manifest["failures"] == []
    assert (tmp_path / "gzip-fixture.html").read_bytes() == publication
    document = json.loads((tmp_path / "gzip-fixture.json").read_text())
    assert document["raw_sha256"] == hashlib.sha256(publication).hexdigest()
    assert corpus_builder.verify(tmp_path)["valid"] is True
