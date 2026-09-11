#!/usr/bin/env python3
"""Run the project-specific page-retrieval benchmark in an isolated database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SOURCE_PATHS = ("rag_project_benchmark.py", "app/rag.py", "app/rag_chunks.py", "app/rag_benchmark_dataset.py")


def _mean(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return round(sum(values) / len(values), 4) if values else None


def _template_bootstrap_ci(
    rows: list[dict[str, Any]], field: str, *, samples: int = 2000, seed: int = 20260909,
) -> list[float] | None:
    """Bootstrap template clusters so twelve cloned cases are not treated as independent."""
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(field) is not None:
            grouped[str(row["template_id"])].append(float(row[field]))
    cluster_values = [sum(values) / len(values) for values in grouped.values() if values]
    if not cluster_values:
        return None
    rng = random.Random(seed)
    estimates = sorted(
        sum(rng.choice(cluster_values) for _ in cluster_values) / len(cluster_values)
        for _ in range(samples)
    )
    lower = estimates[int(samples * 0.025)]
    upper = estimates[min(samples - 1, int(samples * 0.975))]
    return [round(lower, 4), round(upper, 4)]


def score_query(
    item: dict[str, Any], hits: list[dict[str, Any]], metrics: dict[str, Any], latency_ms: int,
) -> dict[str, Any]:
    returned_pairs = [(str(hit["name"]), int(hit["page_no"])) for hit in hits]
    returned = set(returned_pairs)
    expected = {(str(gold["document"]), int(gold["page"])) for gold in item["expected"]}
    relevant = returned & expected
    expected_documents = {document for document, _ in expected}
    returned_expected_document_pages = [
        pair for pair in returned_pairs if pair[0] in expected_documents
    ]
    ranks = [rank for rank, pair in enumerate(returned_pairs, 1) if pair in expected]
    answerable = bool(expected)
    recall = len(relevant) / len(expected) if answerable else None
    precision = len(relevant) / len(returned_pairs) if answerable and returned_pairs else 0.0 if answerable else None
    within_document_precision = (
        sum(pair in expected for pair in returned_expected_document_pages)
        / len(returned_expected_document_pages)
        if answerable and returned_expected_document_pages
        else None
    )
    reciprocal_rank = 1 / min(ranks) if answerable and ranks else 0.0 if answerable else None
    complete_recall = recall == 1.0 if answerable else None
    correctly_empty = not hits if not answerable else None
    quote_presence_rate = sum(bool(hit.get("quote")) for hit in hits) / len(hits) if hits else 0.0
    return {
        "id": item["id"],
        "template_id": item["template_id"],
        "case_key": item["case_key"],
        "challenge": item["challenge"],
        "query": item["query"],
        "answerable": answerable,
        "expected": item["expected"],
        "returned": [
            {"document": hit["name"], "page": hit["page_no"], "rank": hit["rank"]}
            for hit in hits
        ],
        "recall_at_k": round(recall, 4) if recall is not None else None,
        "mrr": round(reciprocal_rank, 4) if reciprocal_rank is not None else None,
        "page_precision_at_k": round(precision, 4) if precision is not None else None,
        "within_document_page_precision": (
            round(within_document_precision, 4) if within_document_precision is not None else None
        ),
        "complete_recall": complete_recall,
        "unanswerable_correctly_empty": correctly_empty,
        "passed": complete_recall if answerable else correctly_empty,
        "quote_presence_rate": round(quote_presence_rate, 4),
        "answer_citation_faithfulness": None,
        "latency_ms": latency_ms,
        "retrieval": metrics,
    }


def _summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]
    return {
        "queries": len(rows),
        "recall_at_k": _mean(answerable, "recall_at_k"),
        "mrr": _mean(answerable, "mrr"),
        "page_precision_at_k": _mean(answerable, "page_precision_at_k"),
        "within_document_page_precision": _mean(answerable, "within_document_page_precision"),
        "within_document_page_precision_query_count": sum(
            row.get("within_document_page_precision") is not None for row in answerable
        ),
        "complete_recall_rate": _mean(answerable, "complete_recall"),
        "unanswerable_empty_rate": _mean(unanswerable, "unanswerable_correctly_empty"),
    }


def paired_comparison(baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> dict[str, Any]:
    """Reject mismatched questions before computing paired cluster deltas."""
    old = {row["id"]: row for row in baseline}
    new = {row["id"]: row for row in candidate}
    if len(old) != len(baseline) or len(new) != len(candidate) or old.keys() != new.keys():
        raise ValueError("Paired comparison requires unique identical question IDs")
    fields = (
        "recall_at_k",
        "mrr",
        "page_precision_at_k",
        "within_document_page_precision",
        "complete_recall",
    )
    deltas = []
    for key, row in new.items():
        previous = old[key]
        if any(row[field] != previous[field] for field in ("query", "expected", "template_id", "case_key")):
            raise ValueError("Paired comparison inputs or labels differ")
        delta = {"template_id": row["template_id"]}
        for field in fields:
            current_value = row.get(field)
            previous_value = previous.get(field)
            delta[field] = (
                float(current_value) - float(previous_value)
                if current_value is not None and previous_value is not None
                else None
            )
        deltas.append(delta)
    return {
        field: {
            "delta": _mean(deltas, field),
            "cluster_bootstrap_95ci": _template_bootstrap_ci(deltas, field),
            "paired_query_count": sum(delta.get(field) is not None for delta in deltas),
        }
        for field in fields
    }


def _runtime_dimensions(row: dict[str, Any]) -> dict[str, Any]:
    retrieval = row["retrieval"]
    embedding = retrieval.get("embedding")
    reranker = retrieval.get("reranker")
    embedding_backend = embedding.get("backend") if isinstance(embedding, dict) else None
    degraded = retrieval.get("degraded")
    reranker_enabled = reranker.get("enabled") if isinstance(reranker, dict) else None
    return {
        "embedding_backend": str(embedding_backend or "unknown"),
        "degraded": degraded if isinstance(degraded, bool) else None,
        "reranker_enabled": reranker_enabled if isinstance(reranker_enabled, bool) else None,
    }


def _runtime_group_name(dimensions: dict[str, Any]) -> str:
    degraded = "unknown" if dimensions["degraded"] is None else str(dimensions["degraded"]).lower()
    reranker = (
        "unknown"
        if dimensions["reranker_enabled"] is None
        else str(dimensions["reranker_enabled"]).lower()
    )
    return (
        f"embedding={dimensions['embedding_backend']}|"
        f"degraded={degraded}|"
        f"reranker={reranker}"
    )


def summarize(
    results: list[dict[str, Any]], *, dataset_version: str, k: int, configuration: dict[str, str],
    dataset_sha256: str,
) -> dict[str, Any]:
    if not results:
        raise ValueError("results must not be empty")
    if k <= 0:
        raise ValueError("k must be greater than zero")
    answerable = [row for row in results if row["answerable"]]
    unanswerable = [row for row in results if not row["answerable"]]
    by_challenge = {
        challenge: _summarize_group([row for row in results if row["challenge"] == challenge])
        for challenge in sorted({str(row["challenge"]) for row in results})
    }
    runtime_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    runtime_dimensions: dict[str, dict[str, Any]] = {}
    for row in results:
        dimensions = _runtime_dimensions(row)
        name = _runtime_group_name(dimensions)
        runtime_rows[name].append(row)
        runtime_dimensions[name] = dimensions
    by_runtime = {
        name: {**runtime_dimensions[name], **_summarize_group(runtime_rows[name])}
        for name in sorted(runtime_rows)
    }
    backends = Counter(
        str(row["retrieval"].get("embedding", {}).get("backend") or "unknown") for row in results
    )
    reranker_enabled = sum(bool(row["retrieval"].get("reranker", {}).get("enabled")) for row in results)
    source_hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_PATHS
    }
    try:
        git_revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        git_dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip())
    except (OSError, subprocess.CalledProcessError):
        git_revision, git_dirty = "unavailable", None
    return {
        "dataset": dataset_version,
        "dataset_sha256": dataset_sha256,
        "questions": len(results),
        "answerable_queries": len(answerable),
        "unanswerable_queries": len(unanswerable),
        "k": k,
        "recall_at_k": _mean(answerable, "recall_at_k"),
        "mrr": _mean(answerable, "mrr"),
        "page_precision_at_k": _mean(answerable, "page_precision_at_k"),
        "within_document_page_precision": _mean(answerable, "within_document_page_precision"),
        "within_document_page_precision_query_count": sum(
            row.get("within_document_page_precision") is not None for row in answerable
        ),
        "complete_recall_rate": _mean(answerable, "complete_recall"),
        "unanswerable_empty_rate": _mean(unanswerable, "unanswerable_correctly_empty"),
        "quote_presence_rate": _mean(results, "quote_presence_rate"),
        "answer_citation_faithfulness": None,
        "template_cluster_bootstrap_95ci": {
            field: _template_bootstrap_ci(answerable, field)
            for field in (
                "recall_at_k",
                "mrr",
                "page_precision_at_k",
                "within_document_page_precision",
                "complete_recall",
            )
        },
        "by_challenge": by_challenge,
        "by_runtime": by_runtime,
        "configuration": configuration,
        "observed": {
            "embedding_backends": dict(sorted(backends.items())),
            "degraded_queries": sum(bool(row["retrieval"].get("degraded")) for row in results),
            "reranker_enabled_queries": reranker_enabled,
        },
        "average_latency_ms": round(sum(row["latency_ms"] for row in results) / len(results)),
        "metric_notes": {
            "within_document_page_precision": "conditional on retrieving a gold document; report with recall because cross-document misses are excluded and no retrieved gold-document page is null",
            "unanswerable_empty_rate": "retrieval-empty diagnostic only; related evidence may support an answer of insufficient evidence; not answer abstention accuracy",
            "quote_presence_rate": "presence only; not citation grounding or entailment",
            "confidence_interval": "template-cluster bootstrap; repeated case variants are not independent samples",
        },
        "source_sha256": source_hashes,
        "git_revision": git_revision,
        "git_dirty": git_dirty,
        "created_at": datetime.now().astimezone().isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="LexVault 240题项目专用RAG评测")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--suite", choices=("classic", "challenges"), default="classic")
    parser.add_argument("--page-children", action="store_true")
    parser.add_argument("--embedding-mode", choices=("hashed-local", "model"), default="hashed-local")
    parser.add_argument("--reranker", choices=("off", "on"), default="off")
    args = parser.parse_args()
    if args.k <= 0:
        parser.error("--k must be greater than zero")
    output_dir = args.output_dir or Path("output") / "test-runs" / datetime.now().strftime("%Y%m%d-%H%M%S") / "rag-240"
    output_dir.mkdir(parents=True, exist_ok=True)
    configuration = {"embedding_mode": args.embedding_mode, "reranker": args.reranker,
                     "page_children": str(args.page_children)}
    with tempfile.TemporaryDirectory(prefix="lexvault-rag240-") as data_dir:
        os.environ["LAW_REVIEW_DATA_DIR"] = data_dir
        from app.db import init_db, now, transaction
        from app.rag import HybridRetriever
        from app.rag_benchmark_dataset import (
            DATASET_VERSION, CHALLENGE_VERSION, build_rag_benchmark, build_challenge_benchmark,
        )

        init_db(seed=False)
        dataset = build_challenge_benchmark() if args.suite == "challenges" else build_rag_benchmark()
        dataset_sha256 = hashlib.sha256(
            json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        by_case: dict[str, list[dict[str, Any]]] = {}
        for item in dataset:
            by_case.setdefault(item["case_key"], item["documents"])
        case_ids: dict[str, int] = {}
        with transaction() as conn:
            for case_key, documents in by_case.items():
                case_id = conn.execute(
                    "INSERT INTO cases(title,case_no,case_type,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (f"{case_key}检索评测案", case_key, "测试", now(), now()),
                ).lastrowid
                if case_id is None:
                    raise RuntimeError("benchmark_case_insert_failed")
                case_ids[case_key] = case_id
                for document in documents:
                    doc_id = conn.execute(
                        """INSERT INTO documents(case_id,name,pages,doc_type,created_at,updated_at)
                           VALUES (?,?,?,?,?,?)""",
                        (case_id, document["name"], len(document["pages"]), "测试材料", now(), now()),
                    ).lastrowid
                    if doc_id is None:
                        raise RuntimeError("benchmark_document_insert_failed")
                    for page_no, text in enumerate(document["pages"], 1):
                        conn.execute(
                            "INSERT INTO pages(document_id,page_no,text,summary) VALUES (?,?,?,?)",
                            (doc_id, page_no, text, text[:140]),
                        )
        prefer_remote = args.embedding_mode == "model"
        use_reranker = args.reranker == "on"
        results = []
        retrievers = {
            key: HybridRetriever(
                value,
                prefer_remote_embeddings=prefer_remote,
                use_neural_reranker=use_reranker,
                use_page_children=args.page_children,
            )
            for key, value in case_ids.items()
        }
        total = len(dataset)
        for position, item in enumerate(dataset, 1):
            started = time.perf_counter()
            hits, metrics = retrievers[item["case_key"]].retrieve(item["query"], args.k)
            latency_ms = round((time.perf_counter() - started) * 1000)
            record = score_query(item, hits, metrics, latency_ms)
            results.append(record)
            print(f"[{position}/{total}] {item['id']} {'PASS' if record['passed'] else 'MISS'}", flush=True)
        summary = summarize(
            results,
            dataset_version=CHALLENGE_VERSION if args.suite == "challenges" else DATASET_VERSION,
            k=args.k,
            configuration=configuration,
            dataset_sha256=dataset_sha256,
        )
        (output_dir / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"结果目录：{output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
