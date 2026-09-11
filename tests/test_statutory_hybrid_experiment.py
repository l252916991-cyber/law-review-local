from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.legal_corpus import SCHEMA_VERSION, LegalCorpus, split_articles
from app.statutory_index import IndexSpec, StatutoryIndex
from scripts.statutory_hybrid_experiment import build_dataset, build_validity


def write_corpus(tmp_path: Path, docs: list[dict]) -> LegalCorpus:
    entries = []
    for index, doc in enumerate(docs):
        document_id = doc["document_id"]
        payload = {"schema_version": SCHEMA_VERSION, "document_id": document_id,
                   "law_name": doc["law_name"], "aliases": doc.get("aliases", [doc["law_name"]]),
                   "version_date": doc["version_date"], "effective_date": doc.get("effective_date"),
                   "version_status": "test_fixture", "source_url": f"https://example.invalid/{document_id}",
                   "articles": split_articles(doc["text"])}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        (tmp_path / f"{document_id}.json").write_bytes(raw)
        entries.append({"document_file": f"{document_id}.json", "document_sha256": hashlib.sha256(raw).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    return LegalCorpus(tmp_path)


def make_index(corpus: LegalCorpus) -> StatutoryIndex:
    vectors = {f"{doc['document_id']}/{article['article_id']}": [1.0, 0.0]
               for doc in corpus.documents for article in doc["articles"]}
    validity = {doc["document_id"]: {"effective_from": doc["effective_date"] or doc["version_date"],
                                     "effective_to": None, "source": doc["source_url"]}
                for doc in corpus.documents}
    return StatutoryIndex(corpus, IndexSpec("fake-v1", 2, "fixture"), vectors, validity)


def test_build_validity_closes_superseded_version(tmp_path):
    corpus = write_corpus(tmp_path, [
        {"document_id": "law-2006", "law_name": "测试法", "version_date": "2006-01-01",
         "effective_date": "2006-02-01", "text": "第一条 旧。"},
        {"document_id": "law-2017", "law_name": "测试法", "version_date": "2017-01-01",
         "effective_date": "2017-02-01", "text": "第一条 新。"},
    ])
    validity = build_validity(corpus)
    assert validity["law-2006"] == {"effective_from": "2006-02-01", "effective_to": "2017-02-01",
                                    "source": "https://example.invalid/law-2006"}
    assert validity["law-2017"]["effective_to"] is None
    assert validity["law-2017"]["effective_from"] == "2017-02-01"


def test_build_validity_falls_back_to_version_date(tmp_path):
    corpus = write_corpus(tmp_path, [
        {"document_id": "law-a", "law_name": "甲法", "version_date": "2020-03-01",
         "effective_date": None, "text": "第一条 甲。"}])
    assert build_validity(corpus)["law-a"]["effective_from"] == "2020-03-01"


def test_build_dataset_resolves_gold_and_splits(tmp_path):
    # conftest synthesizes every 3-1 answer as 法条:刑法第264条, so a corpus that
    # contains criminal-law/264 yields a fully-scored dataset without the real split.
    corpus = write_corpus(tmp_path, [
        {"document_id": "criminal-law-2023", "law_name": "中华人民共和国刑法",
         "aliases": ["中华人民共和国刑法", "刑法"], "version_date": "2023-01-01",
         "effective_date": "2024-03-01", "text": "第一条 总则。\n第二百六十四条 盗窃。"}])
    index = make_index(corpus)
    dataset = build_dataset("3-1", index, per_split=5, effective_date="2026-01-01")
    assert len(dataset["tasks"]) == 10
    assert {task["split"] for task in dataset["tasks"]} == {"dev", "confirm"}
    assert sum(1 for task in dataset["tasks"] if task["split"] == "dev") == 5
    assert all(task["gold"] == {"criminal-law-2023/264": 1} for task in dataset["tasks"])
    assert all(task["query_effective_date"] == "2026-01-01" for task in dataset["tasks"])
    assert len({task["id"] for task in dataset["tasks"]}) == 10


def test_build_dataset_requires_enough_evaluable_questions(tmp_path):
    corpus = write_corpus(tmp_path, [
        {"document_id": "other", "law_name": "别法", "version_date": "2020-01-01",
         "effective_date": None, "text": "第一条 无关。"}])
    index = make_index(corpus)
    # Synthetic answers cite 刑法, which this corpus lacks, so nothing is evaluable.
    with pytest.raises(ValueError, match="Not enough evaluable"):
        build_dataset("3-1", index, per_split=5, effective_date="2026-01-01")
