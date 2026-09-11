from __future__ import annotations

from app.rag_benchmark_dataset import LONG_PAGE_VERSION, build_long_page_benchmark
from rag_project_benchmark import paired_comparison, paired_run_comparison
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


def test_paired_run_comparison_requires_only_page_children_to_differ():
    row = score_query(item([{"document": "a.txt", "page": 1}]), [], metrics(), 0)
    baseline_summary = {
        "dataset": LONG_PAGE_VERSION,
        "dataset_sha256": "frozen-hash",
        "k": 2,
        "configuration": {
            "embedding_mode": "hashed-local",
            "reranker": "off",
            "page_children": "False",
        },
    }
    candidate_summary = {
        **baseline_summary,
        "configuration": {**baseline_summary["configuration"], "page_children": "True"},
    }

    report = paired_run_comparison(baseline_summary, candidate_summary, [row], [row])

    assert report["comparison"] == "two_complete_retrieval_paths_diagnostic"
    assert report["causal_attribution"] == "not_a_pure_chunking_ablation"
    assert report["controls"]["k"] == 2
    assert report["controls"]["dataset_sha256"] == "frozen-hash"
    assert report["metrics"]["recall_at_k"]["paired_query_count"] == 1

    for changed, message in (
        ({**candidate_summary, "k": 3}, "identical k"),
        ({**candidate_summary, "dataset_sha256": "changed"}, "dataset_sha256"),
        (
            {
                **candidate_summary,
                "configuration": {**candidate_summary["configuration"], "reranker": "on"},
            },
            "run mode",
        ),
        ({**candidate_summary, "configuration": baseline_summary["configuration"]}, "page-level baseline"),
    ):
        with pytest.raises(ValueError, match=message):
            paired_run_comparison(baseline_summary, changed, [row], [row])

    missing_hash = {key: value for key, value in candidate_summary.items() if key != "dataset_sha256"}
    with pytest.raises(ValueError, match="present dataset_sha256"):
        paired_run_comparison(baseline_summary, missing_hash, [row], [row])

    changed_runtime = {**row, "retrieval": full_metrics()}
    with pytest.raises(ValueError, match="actual runtime"):
        paired_run_comparison(baseline_summary, candidate_summary, [row], [changed_runtime])

    missing_runtime = {**row, "retrieval": {"embedding": {"backend": "hashed-local"}}}
    with pytest.raises(ValueError, match="runtime telemetry"):
        paired_run_comparison(baseline_summary, candidate_summary, [row], [missing_runtime])


def test_long_page_dataset_is_frozen_and_fact_grounded():
    rows = build_long_page_benchmark()

    assert len(rows) == 12
    assert len({row["id"] for row in rows}) == 12
    assert len({row["query"] for row in rows}) == 12
    assert len({row["cluster_id"] for row in rows}) == 4
    assert {row["template_id"] for row in rows} == {row["cluster_id"] for row in rows}
    assert {row["diagnostic_provenance"] for row in rows} == {
        "synthetic_long_page_diagnostic"
    }
    assert {row["challenge"] for row in rows} == {
        "long_page_dilution",
        "similar_neighbor_interference",
        "cross_page_two_aspects",
    }

    for row in rows:
        pages_by_document = {
            document["name"]: document["pages"] for document in row["documents"]
        }
        expected = {(gold["document"], gold["page"]) for gold in row["expected"]}
        facts = {(fact["document"], fact["page"]) for fact in row["gold_facts"]}
        assert expected == facts
        for fact in row["gold_facts"]:
            pages = pages_by_document[fact["document"]]
            assert 1 <= fact["page"] <= len(pages)
            page_text = pages[fact["page"] - 1]
            assert page_text.count(fact["text"]) == 1

    long_page_rows = [row for row in rows if row["challenge"] == "long_page_dilution"]
    assert len(long_page_rows) == 4
    for row in long_page_rows:
        fact = row["gold_facts"][0]
        page_text = row["documents"][0]["pages"][fact["page"] - 1]
        fact_offset = page_text.index(fact["text"])
        assert len(page_text) > 1200
        assert fact_offset >= 400
        assert page_text.count("\n\n") >= 8

    cross_page_rows = [row for row in rows if row["challenge"] == "cross_page_two_aspects"]
    assert len(cross_page_rows) == 4
    assert all(len({gold["page"] for gold in row["expected"]}) == 2 for row in cross_page_rows)


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
