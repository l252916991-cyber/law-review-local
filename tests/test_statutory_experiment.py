from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.legal_corpus import LegalCorpus, SCHEMA_VERSION, split_articles
from app.statutory_benchmark import evaluate, metrics, paired_delta
from app.statutory_hybrid import MODES, StatutoryHybrid
from app.statutory_index import IndexSpec, StatutoryIndex


@pytest.fixture
def corpus(tmp_path):
    entries = []
    for key, law, text in [("a", "合同法", "第一条 合同履行约定。"), ("b", "劳动法", "第一条 劳动报酬支付。")]:
        doc = {"schema_version": SCHEMA_VERSION, "document_id": key, "law_name": law, "aliases": [law],
               "version_date": "2020-01-01", "effective_date": "2020-02-01",
               "version_status": "currentness_not_asserted", "source_url": "https://example.invalid/law",
               "articles": split_articles(text)}
        raw = json.dumps(doc).encode()
        (tmp_path / f"{key}.json").write_bytes(raw)
        entries.append({"document_file": f"{key}.json", "document_sha256": hashlib.sha256(raw).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    return LegalCorpus(tmp_path)


def validity():
    return {key: {"effective_from": "2020-02-01", "effective_to": None, "source": "fixture-only"} for key in ("a", "b")}


def make_index(corpus, **kwargs):
    return StatutoryIndex(corpus, IndexSpec("fake-v1", 2, "fixture"),
                          {"a/1": [1., 0.], "b/1": [0., 1.]}, kwargs.get("validity", validity()))


class FakeEmbedding:
    model = "fake-v1"
    last_failure = None

    def embed(self, texts):
        return [[1., 0.] for _ in texts], "fixture"


class FakeRerank:
    model = "fake-rerank-v1"
    last_failure = None

    def score(self, query, documents):
        return [float("劳动" in text) for text in documents]


def test_four_modes_and_filters(corpus):
    engine = StatutoryHybrid(make_index(corpus), embedding=FakeEmbedding(), reranker=FakeRerank())
    for mode in MODES:
        result = engine.search("合同", mode=mode)
        assert result["available"] and not result["failure"]
        assert result["embedding_calls"] == (mode != "lexical")
        assert result["hits"][0]["id"] == ("b/1" if mode == "rerank" else "a/1")
    result = engine.search("合同", mode="dense", explicit_law="劳动法")
    assert [hit["id"] for hit in result["hits"]] == ["b/1"]
    result = engine.search("合同", mode="dense", inferred_law="劳动法")
    assert {hit["id"] for hit in result["hits"]} == {"a/1", "b/1"}
    assert next(hit["score"] for hit in result["hits"] if hit["id"] == "b/1") > 0
    assert not engine.search("合同", explicit_law="不存在")["available"]


def test_effective_intervals_and_missing_metadata(corpus):
    meta = validity()
    meta["a"]["effective_to"] = "2021-01-01"
    index = make_index(corpus, validity=meta)
    assert index.eligible(query_effective_date="2020-02-01")[0] == ["a/1", "b/1"]
    assert index.eligible(query_effective_date="2021-01-01")[0] == ["b/1"]
    assert index.eligible(query_effective_date="2020-01-31")[0] == []
    assert index.eligible(today=date(2022, 1, 1))[0] == ["b/1"]
    unknown = make_index(corpus, validity={})
    assert unknown.eligible()[1] == ["unverified_validity:a", "unverified_validity:b"]
    assert not StatutoryHybrid(unknown).search("合同")["available"]
    meta["a"]["effective_from"] = "2019-01-01"
    assert "unverified_validity:a" in make_index(corpus, validity=meta).eligible()[1]
    corpus.documents[1]["law_name"] = corpus.documents[0]["law_name"]
    assert make_index(corpus).eligible()[1] == ["ambiguous_versions:合同法"]


@pytest.mark.parametrize("field,value", [("embedding_model", "other"), ("embedding_dim", 3),
    ("embedding_backend", "other"), ("normalization", "none"), ("parser_version", "v2")])
def test_cache_fingerprint_spec(corpus, tmp_path, field, value):
    index = make_index(corpus)
    path = tmp_path / "index.json"
    index.save(path)
    assert StatutoryIndex.load(path, corpus, index.spec, validity()).vectors == index.vectors
    with pytest.raises(ValueError, match="mismatch"):
        StatutoryIndex.load(path, corpus, replace(index.spec, **{field: value}), validity())


def test_cache_corpus_validity_tamper(corpus, tmp_path):
    index = make_index(corpus)
    path = tmp_path / "index.json"
    index.save(path)
    with pytest.raises(ValueError, match="mismatch"):
        StatutoryIndex.load(path, corpus, index.spec, {})
    corpus.documents[0]["articles"][0]["text"] += "changed"
    with pytest.raises(ValueError, match="mismatch"):
        StatutoryIndex.load(path, corpus, index.spec, validity())
    data = json.loads(path.read_text())
    data["payload"]["vectors"]["a/1"] = [0, 1]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="mismatch"):
        StatutoryIndex.load(path, corpus, index.spec, validity())


@pytest.mark.parametrize("vector", [[0, 0], [1], [float("nan"), 1], [True, 1]])
def test_invalid_vectors(corpus, vector):
    with pytest.raises(ValueError):
        StatutoryIndex(corpus, IndexSpec("fake-v1", 2, "fixture"), {"a/1": vector, "b/1": [0, 1]})


def test_client_failures_not_fallback(corpus):
    engine = StatutoryHybrid(make_index(corpus))
    assert engine.search("合同")["available"]
    assert not engine.search("合同", mode="rrf")["available"]
    fake = FakeEmbedding()
    fake.last_failure = "fallback"
    engine = StatutoryHybrid(make_index(corpus), embedding=fake)
    assert not engine.search("合同", mode="dense")["available"]
    fake.last_failure = None
    fake.model = "mismatch"
    assert engine.search("合同", mode="dense")["embedding_calls"] == 0
    engine = StatutoryHybrid(make_index(corpus), embedding=FakeEmbedding())
    result = engine.search("合同", mode="rerank")
    assert not result["available"] and result["hits"] == []

    class InvalidReranker:
        last_failure = None

        def score(self, query, documents):
            return [float("nan")]

    engine = StatutoryHybrid(make_index(corpus), embedding=FakeEmbedding(), reranker=InvalidReranker())
    result = engine.search("合同", mode="rerank")
    assert not result["available"] and result["rerank_candidates"] == 2
    assert result["failure"] == "ValueError:invalid_rerank_response"

    class WrongBackend(FakeEmbedding):
        def embed(self, texts):
            return [[1., 0.]], "hashed-local"

    result = StatutoryHybrid(make_index(corpus), embedding=WrongBackend()).search("合同", mode="dense")
    assert not result["available"] and result["embedding_calls"] == 1


def dataset():
    return {"fixture": True, "tasks": [
        {"id": "dev-1", "split": "dev", "query": "劳动报酬", "query_effective_date": "2021-01-01", "gold": {"b/1": 2}},
        {"id": "confirm-1", "split": "confirm", "query": "合同履行", "query_effective_date": "2021-01-01", "gold": {"a/1": 2}},
        {"id": "confirm-2", "split": "confirm", "query": "支付劳动报酬", "query_effective_date": "2021-01-01", "gold": {"b/1": 1}},
    ]}


def test_metrics_bootstrap_and_benchmark(corpus):
    assert metrics(["x", "a"], {"a": 1}) == {"hit_at_5": 1., "mrr_at_10": .5, "ndcg_at_10": 1 / __import__("math").log2(3)}
    assert paired_delta([.5, .5], samples=100, seed=1) == {"n": 2, "delta": .5, "ci95": [.5, .5]}
    assert paired_delta([], samples=100, seed=1)["delta"] is None
    config = json.loads((Path(__file__).parents[1] / "benchmarks/statutory-experiment-v1.json").read_text())
    engine = StatutoryHybrid(make_index(corpus), embedding=FakeEmbedding(), reranker=FakeRerank())
    report = evaluate(engine, dataset(), config, split="confirm")
    assert report["split"] == "confirm" and report["summaries"]["dense"]["available"] == 2
    assert report["summaries"]["lexical"]["metrics"]["mrr_at_10"] == 1
    assert report["summaries"]["dense"]["metrics"]["mrr_at_10"] == .75
    assert report["comparisons"]["dense"]["mrr_at_10"]["delta"] == -.25
    assert not report["comparisons"]["rrf"]["preset_gate_passed"]
    assert report["summaries"]["rerank"]["rerank_candidates_per_query"] == 2
    failed = evaluate(StatutoryHybrid(make_index(corpus)), dataset(), config, split="confirm")
    assert failed["summaries"]["dense"]["failure"] == 2
    assert failed["summaries"]["dense"]["metrics"]["mrr_at_10"] is None
    assert failed["comparisons"]["dense"]["mrr_at_10"]["n"] == 0
    bad = dataset()
    bad["tasks"][0]["query"] = bad["tasks"][1]["query"]
    with pytest.raises(ValueError, match="leakage"):
        evaluate(engine, bad, config)


def test_index_search_and_original_lexical_baseline(corpus):
    index = make_index(corpus)
    assert index.search([1, 0], ["b/1"], 1) == [("b/1", 0.0)]
    assert index.search([1, 0], list(index.rows), 1) == [("a/1", 1.0)]
    original = corpus.search("合同 劳动", limit=10)
    result = StatutoryHybrid(index).search("合同 劳动")
    assert [(r["document_id"], r["score"]) for r in result["hits"]] == [
        (r["document_id"], r["retrieval_score"]) for r in original]


def test_joint_gate_and_identity(corpus):
    config = json.loads((Path(__file__).parents[1] / "benchmarks/statutory-experiment-v1.json").read_text())
    engine = StatutoryHybrid(make_index(corpus), embedding=FakeEmbedding(), reranker=FakeRerank())
    report = evaluate(engine, dataset(), config)
    assert report["promotion"] == {"dense": False, "rrf": False, "rerank": False}
    assert report["dev"]["primary_metric"] == "hit_at_5"
    assert report["dev"]["experiment_fingerprint"] == report["confirm"]["experiment_fingerprint"]
    identity = report["dev"]["experiment_fingerprint"]
    engine.soft_boost = .2
    assert evaluate(engine, dataset(), config)["dev"]["experiment_fingerprint"] != identity
    engine.soft_boost = .1
    engine.index.vectors["a/1"] = (0., 1.)
    assert evaluate(engine, dataset(), config)["dev"]["experiment_fingerprint"] != identity
    engine.reranker = None
    assert not evaluate(engine, dataset(), config)["dev"]["identity_complete"]


def test_rerank_ties_keep_rrf_order_and_gold_eligibility(corpus):
    class TiedReranker(FakeRerank):
        def score(self, query, documents):
            return [0.] * len(documents)

    engine = StatutoryHybrid(make_index(corpus), embedding=FakeEmbedding(), reranker=TiedReranker(), soft_boost=1.)
    before = engine.search("劳动", mode="rrf", inferred_law="劳动法")
    after = engine.search("劳动", mode="rerank", inferred_law="劳动法")
    assert before["hits"][0]["id"] == "b/1"
    assert [r["id"] for r in before["hits"]] == [r["id"] for r in after["hits"]]
    config = json.loads((Path(__file__).parents[1] / "benchmarks/statutory-experiment-v1.json").read_text())
    bad = dataset()
    bad["tasks"][0]["explicit_law"] = "合同法"
    with pytest.raises(ValueError, match="eligibility"):
        evaluate(engine, bad, config)
    bad = dataset()
    bad["tasks"][0]["query_effective_date"] = "2019-01-01"
    with pytest.raises(ValueError, match="eligibility"):
        evaluate(engine, bad, config)
