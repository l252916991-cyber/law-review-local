from __future__ import annotations

from rag_project_benchmark import score_query, summarize
from rag_project_benchmark import paired_comparison
import pytest


def item(expected):
    return {
        "id": "RAG-01-Q01",
        "template_id": "Q01",
        "case_key": "RAG-01",
        "challenge": "multi_source" if expected else "hard_unanswerable",
        "query": "测试问题",
        "expected": expected,
    }


def metrics():
    return {
        "degraded": True,
        "embedding": {"backend": "hashed-local"},
        "reranker": {"enabled": False},
    }


def test_answerable_pass_requires_complete_page_recall():
    expected = [{"document": "a.txt", "page": 1}, {"document": "a.txt", "page": 2}]
    hits = [{"name": "a.txt", "page_no": 1, "rank": 1, "quote": "命中"}]

    record = score_query(item(expected), hits, metrics(), 3)

    assert record["recall_at_k"] == 0.5
    assert record["page_precision_at_k"] == 1.0
    assert record["complete_recall"] is False
    assert record["passed"] is False


def test_paired_comparison_rejects_changed_labels():
    row = score_query(item([{"document": "a.txt", "page": 1}]), [], metrics(), 0)
    assert paired_comparison([row], [row])["recall_at_k"]["delta"] == 0
    with pytest.raises(ValueError, match="labels differ"):
        paired_comparison([row], [{**row, "expected": []}])
    with pytest.raises(ValueError, match="unique identical"):
        paired_comparison([row, row], [row])


def test_hard_unanswerable_requires_empty_retrieval():
    hits = [{"name": "similar.txt", "page_no": 1, "rank": 1, "quote": "词面相似"}]

    record = score_query(item([]), hits, metrics(), 3)

    assert record["recall_at_k"] is None
    assert record["mrr"] is None
    assert record["unanswerable_correctly_empty"] is False
    assert record["passed"] is False


def test_summary_reports_observed_runtime_and_cluster_interval():
    first = score_query(
        item([{"document": "a.txt", "page": 1}]),
        [{"name": "a.txt", "page_no": 1, "rank": 1, "quote": "命中"}],
        metrics(),
        3,
    )
    second_item = {**item([]), "id": "RAG-01-Q02", "template_id": "Q02"}
    second = score_query(second_item, [], metrics(), 5)

    report = summarize(
        [first, second],
        dataset_version="fixture-v1",
        k=5,
        configuration={"embedding_mode": "hashed-local", "reranker": "off"},
        dataset_sha256="fixture",
    )

    assert report["complete_recall_rate"] == 1.0
    assert report["unanswerable_empty_rate"] == 1.0
    assert report["observed"]["embedding_backends"] == {"hashed-local": 2}
    assert report["observed"]["degraded_queries"] == 2
    assert report["observed"]["reranker_enabled_queries"] == 0
    assert report["template_cluster_bootstrap_95ci"]["recall_at_k"] == [1.0, 1.0]
