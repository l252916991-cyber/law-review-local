from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.legal_corpus import SCHEMA_VERSION, LegalCorpus, split_articles
from app.statutory_gold import citations, gold_citations
from scripts.statutory_gold_report import evaluate_task, resolve


@pytest.mark.parametrize("text,expected", [
    ("法律依据:《中华人民共和国道路交通安全法》第九十一条饮酒后驾驶机动车的", [("中华人民共和国道路交通安全法", 91)]),
    ("根据农民专业合作社法第七十条的规定，登记机关可以责令其改正", [("农民专业合作社法", 70)]),
    ("根据《民法典》第二百零九条不动产物权的设立", [("民法典", 209)]),
    ("根据规定:醉酒驾驶的构成了犯罪，应当以危险驾驶罪定罪处罚", []),
])
def test_citations_extraction(text, expected):
    assert citations(text) == expected


def test_one_one_question_strips_category_prefix():
    question = "民法商法农民专业合作社法第三十三条的内容是什么？"
    assert gold_citations("1-1", question, answer="答案:任意内容") == [("农民专业合作社法", 33)]


def test_gold_citations_source_per_task():
    assert gold_citations("3-1", "事实:...", "法条:刑法第264条") == [("刑法", 264)]
    assert gold_citations("3-8", "问题", "回答:x 法律依据:《民法典》第一百六十一条") == [("民法典", 161)]
    with pytest.raises(ValueError, match="No statutory gold"):
        gold_citations("2-6", "问", "答")


@pytest.mark.parametrize("task_id,limit", [("1-1", 20), ("3-1", 20), ("3-2", 20), ("3-8", 20)])
def test_extraction_covers_pinned_lawbench(task_id, limit):
    # conftest replaces LawBench with synthetic rows for offline CI; read the
    # pinned files directly and skip when the real dataset is not checked out.
    path = Path(__file__).resolve().parents[1] / "benchmarks/lawbench/zero_shot" / f"{task_id}.json"
    if not path.exists():
        pytest.skip("pinned LawBench dataset not present")
    rows = json.loads(path.read_text(encoding="utf-8"))[:limit]
    with_gold = [row for row in rows if gold_citations(task_id, row["question"], row["answer"])]
    assert len(with_gold) >= 10  # the pinned splits carry extractable citations


@pytest.fixture
def corpus(tmp_path):
    entries = []
    for key, law, aliases, text in [
        ("criminal", "中华人民共和国刑法", ["中华人民共和国刑法", "刑法"], "第一条 总则。\n第二百六十四条 盗窃公私财物。"),
        ("civil", "中华人民共和国民法典", ["中华人民共和国民法典", "民法典"], "第一条 保护民事权益。\n第二百零九条 不动产物权登记。"),
    ]:
        doc = {"schema_version": SCHEMA_VERSION, "document_id": key, "law_name": law, "aliases": aliases,
               "version_date": "2023-01-01", "effective_date": None, "version_status": "test_fixture",
               "source_url": "https://example.invalid/law", "articles": split_articles(text)}
        name = f"{key}.json"
        raw = json.dumps(doc, ensure_ascii=False).encode()
        (tmp_path / name).write_bytes(raw)
        entries.append({"document_file": name, "document_sha256": hashlib.sha256(raw).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    return [str(tmp_path)]


def test_resolve_maps_aliases_and_reports_missing(corpus):
    from scripts.statutory_gold_report import alias_index

    index = alias_index([LegalCorpus(corpus[0])])
    gold, missing = resolve([("刑法", 264), ("不存在法", 1)], index)
    assert gold == {"criminal/264": 1}
    assert missing == ["不存在法第1条"]


def test_evaluate_task_scores_retrieval(corpus):
    rows = [{"question": "事实:被告人秘密窃取财物。", "answer": "法条:刑法第264条"},
            {"question": "事实:另一案件。", "answer": "无法条"}]
    report = evaluate_task("3-1", rows, [LegalCorpus(corpus[0])], corpus)
    assert report["questions"] == 2 and report["scored"] == 1
    assert report["metrics"]["hit_at_5"] == 1.0 and report["metrics"]["mrr_at_10"] == 1.0
