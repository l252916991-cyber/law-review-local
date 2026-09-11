from __future__ import annotations

from rag_project_benchmark import paired_comparison
from rag_project_benchmark import score_query, summarize
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


def full_metrics():
    return {
        "degraded": False,
        "embedding": {"backend": "omlx"},
        "reranker": {"enabled": True},
    }


def test_answerable_pass_requires_complete_page_recall():
    expected = [{"document": "a.txt", "page": 1}, {"document": "a.txt", "page": 2}]
    hits = [{"name": "a.txt", "page_no": 1, "rank": 1, "quote": "命中"}]

    record = score_query(item(expected), hits, metrics(), 3)

    assert record["recall_at_k"] == 0.5
    assert record["page_precision_at_k"] == 1.0
    assert record["within_document_page_precision"] == 1.0
    assert record["complete_recall"] is False
    assert record["passed"] is False


def test_paired_comparison_rejects_changed_labels():
    row = score_query(item([{"document": "a.txt", "page": 1}]), [], metrics(), 0)
    assert paired_comparison([row], [row])["recall_at_k"]["delta"] == 0
    assert paired_comparison([row], [row])["recall_at_k"]["paired_query_count"] == 1
    with pytest.raises(ValueError, match="labels differ"):
        paired_comparison([row], [{**row, "expected": []}])
    with pytest.raises(ValueError, match="unique identical"):
        paired_comparison([row, row], [row])


def test_hard_unanswerable_requires_empty_retrieval():
    hits = [{"name": "similar.txt", "page_no": 1, "rank": 1, "quote": "词面相似"}]

    record = score_query(item([]), hits, metrics(), 3)

    assert record["recall_at_k"] is None
    assert record["mrr"] is None
    assert record["within_document_page_precision"] is None
    assert record["unanswerable_correctly_empty"] is False
    assert record["passed"] is False


def test_within_document_page_precision_excludes_other_documents():
    expected = [
        {"document": "gold.txt", "page": 1},
        {"document": "gold.txt", "page": 3},
    ]
    hits = [
        {"name": "gold.txt", "page_no": 1, "rank": 1, "quote": "命中"},
        {"name": "gold.txt", "page_no": 2, "rank": 2, "quote": "同文档错页"},
        {"name": "other.txt", "page_no": 1, "rank": 3, "quote": "跨文档"},
    ]

    record = score_query(item(expected), hits, metrics(), 3)

    assert record["recall_at_k"] == 0.5
    assert record["page_precision_at_k"] == 0.3333
    assert record["within_document_page_precision"] == 0.5


def test_within_document_page_precision_is_null_without_gold_document_hit():
    expected = [{"document": "gold.txt", "page": 1}]
    hits = [{"name": "other.txt", "page_no": 1, "rank": 1, "quote": "跨文档"}]

    record = score_query(item(expected), hits, metrics(), 3)
    empty_record = score_query(item(expected), [], metrics(), 3)

    assert record["page_precision_at_k"] == 0.0
    assert record["within_document_page_precision"] is None
    assert empty_record["page_precision_at_k"] == 0.0
    assert empty_record["within_document_page_precision"] is None


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
    runtime = report["by_runtime"]["embedding=hashed-local|degraded=true|reranker=false"]
    assert runtime["embedding_backend"] == "hashed-local"
    assert runtime["degraded"] is True
    assert runtime["reranker_enabled"] is False
    assert runtime["queries"] == 2
    assert runtime["within_document_page_precision"] == 1.0
    assert runtime["within_document_page_precision_query_count"] == 1


def test_summary_separates_mixed_observed_runtimes():
    expected = [{"document": "a.txt", "page": 1}]
    hit = [{"name": "a.txt", "page_no": 1, "rank": 1, "quote": "命中"}]
    degraded = score_query(item(expected), hit, metrics(), 3)
    full_item = {**item(expected), "id": "RAG-02-Q01", "case_key": "RAG-02"}
    full = score_query(full_item, hit, full_metrics(), 4)

    report = summarize(
        [degraded, full],
        dataset_version="fixture-v1",
        k=5,
        configuration={"embedding_mode": "model", "reranker": "on"},
        dataset_sha256="fixture",
    )

    assert list(report["by_runtime"]) == [
        "embedding=hashed-local|degraded=true|reranker=false",
        "embedding=omlx|degraded=false|reranker=true",
    ]
    assert report["by_runtime"]["embedding=hashed-local|degraded=true|reranker=false"]["queries"] == 1
    assert report["by_runtime"]["embedding=omlx|degraded=false|reranker=true"]["queries"] == 1
    assert report["observed"]["degraded_queries"] == 1
    assert report["observed"]["reranker_enabled_queries"] == 1


@pytest.mark.parametrize("unknown_metrics", [
    {"embedding": {"backend": "omlx"}},
    {
        "degraded": None,
        "embedding": {"backend": "omlx"},
        "reranker": {"enabled": None},
    },
])
def test_summary_does_not_merge_unknown_runtime_telemetry_with_explicit_false(unknown_metrics):
    expected = [{"document": "a.txt", "page": 1}]
    hit = [{"name": "a.txt", "page_no": 1, "rank": 1, "quote": "命中"}]
    explicit = score_query(item(expected), hit, {
        "degraded": False,
        "embedding": {"backend": "omlx"},
        "reranker": {"enabled": False},
    }, 3)
    unknown_item = {**item(expected), "id": "RAG-02-Q01", "case_key": "RAG-02"}
    unknown = score_query(unknown_item, hit, unknown_metrics, 4)

    report = summarize(
        [explicit, unknown],
        dataset_version="fixture-v1",
        k=5,
        configuration={"embedding_mode": "model", "reranker": "off"},
        dataset_sha256="fixture",
    )

    assert set(report["by_runtime"]) == {
        "embedding=omlx|degraded=false|reranker=false",
        "embedding=omlx|degraded=unknown|reranker=unknown",
    }
    unknown_group = report["by_runtime"]["embedding=omlx|degraded=unknown|reranker=unknown"]
    assert unknown_group["degraded"] is None
    assert unknown_group["reranker_enabled"] is None
    assert unknown_group["queries"] == 1


def test_new_metric_ignores_missing_legacy_values_in_summaries_and_comparison():
    expected = [{"document": "a.txt", "page": 1}]
    hit = [{"name": "a.txt", "page_no": 1, "rank": 1, "quote": "命中"}]
    current = score_query(item(expected), hit, metrics(), 3)
    legacy = {key: value for key, value in current.items() if key != "within_document_page_precision"}

    report = summarize(
        [legacy],
        dataset_version="fixture-v1",
        k=5,
        configuration={"embedding_mode": "hashed-local", "reranker": "off"},
        dataset_sha256="fixture",
    )
    comparison = paired_comparison([legacy], [current])

    assert report["within_document_page_precision"] is None
    assert report["template_cluster_bootstrap_95ci"]["within_document_page_precision"] is None
    runtime = report["by_runtime"]["embedding=hashed-local|degraded=true|reranker=false"]
    assert runtime["within_document_page_precision"] is None
    assert comparison["within_document_page_precision"] == {
        "delta": None,
        "cluster_bootstrap_95ci": None,
        "paired_query_count": 0,
    }


@pytest.mark.parametrize(("results", "k", "message"), [([], 5, "results"), ([{}], 0, "k")])
def test_summary_rejects_invalid_benchmark_boundaries(results, k, message):
    with pytest.raises(ValueError, match=message):
        summarize(
            results,
            dataset_version="fixture-v1",
            k=k,
            configuration={},
            dataset_sha256="fixture",
        )
