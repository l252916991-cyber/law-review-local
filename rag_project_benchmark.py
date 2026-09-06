#!/usr/bin/env python3
"""Run the 240-question project-specific RAG benchmark in an isolated database."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="LexVault 240题项目专用RAG评测")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    output_dir = args.output_dir or Path("output") / "test-runs" / datetime.now().strftime("%Y%m%d-%H%M%S") / "rag-240"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lexvault-rag240-") as data_dir:
        os.environ["LAW_REVIEW_DATA_DIR"] = data_dir
        from app.db import init_db, now, transaction
        from app.rag import HybridRetriever
        from app.rag_benchmark_dataset import build_rag_benchmark

        init_db(seed=False)
        dataset = build_rag_benchmark()
        by_case = {}
        for item in dataset:
            by_case.setdefault(item["case_key"], item["documents"])
        case_ids = {}
        with transaction() as conn:
            for case_key, documents in by_case.items():
                case_id = conn.execute(
                    "INSERT INTO cases(title,case_no,case_type,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (f"{case_key}检索评测案", case_key, "测试", now(), now()),
                ).lastrowid
                case_ids[case_key] = case_id
                for document in documents:
                    doc_id = conn.execute(
                        """INSERT INTO documents(case_id,name,pages,doc_type,created_at,updated_at)
                           VALUES (?,?,?,?,?,?)""",
                        (case_id, document["name"], len(document["pages"]), "测试材料", now(), now()),
                    ).lastrowid
                    for page_no, text in enumerate(document["pages"], 1):
                        conn.execute(
                            "INSERT INTO pages(document_id,page_no,text,summary) VALUES (?,?,?,?)",
                            (doc_id, page_no, text, text[:140]),
                        )
        results = []
        retrievers = {key: HybridRetriever(value, prefer_remote_embeddings=False) for key, value in case_ids.items()}
        for position, item in enumerate(dataset, 1):
            started = time.perf_counter()
            hits, metrics = retrievers[item["case_key"]].retrieve(item["query"], args.k)
            latency_ms = round((time.perf_counter() - started) * 1000)
            returned = {(hit["name"], hit["page_no"]) for hit in hits}
            expected = {(gold["document"], gold["page"]) for gold in item["expected"]}
            ranks = [rank for rank, hit in enumerate(hits, 1) if (hit["name"], hit["page_no"]) in expected]
            if expected:
                recall = len(returned & expected) / len(expected)
                reciprocal_rank = 1 / min(ranks) if ranks else 0.0
                passed = bool(ranks)
            else:
                # Negative questions pass only if none of their impossible concepts
                # appears in the returned evidence text.
                forbidden = [term for term in ("境外", "加密货币", "火星", "采矿许可证") if term in item["query"]]
                passed = not any(term in hit["text"] for hit in hits for term in forbidden)
                recall, reciprocal_rank = float(passed), float(passed)
            record = {
                "id": item["id"], "case_key": item["case_key"], "query": item["query"],
                "answerable": item["answerable"], "expected": item["expected"],
                "returned": [{"document": hit["name"], "page": hit["page_no"], "rank": hit["rank"]} for hit in hits],
                "recall_at_k": round(recall, 4), "mrr": round(reciprocal_rank, 4), "passed": passed,
                "citation_coverage": sum(bool(hit.get("quote")) for hit in hits) / len(hits) if hits else 0.0,
                "latency_ms": latency_ms, "retrieval": metrics,
            }
            results.append(record)
            print(f"[{position}/240] {item['id']} {'PASS' if passed else 'MISS'}", flush=True)
        answerable = [row for row in results if row["answerable"]]
        summary = {
            "dataset": "lexvault-rag-240-v1", "questions": len(results), "k": args.k,
            "recall_at_k": round(sum(row["recall_at_k"] for row in answerable) / len(answerable), 4),
            "mrr": round(sum(row["mrr"] for row in answerable) / len(answerable), 4),
            "citation_coverage": round(sum(row["citation_coverage"] for row in results) / len(results), 4),
            "negative_accuracy": round(sum(row["passed"] for row in results if not row["answerable"]) / 24, 4),
            "average_latency_ms": round(sum(row["latency_ms"] for row in results) / len(results)),
            "created_at": datetime.now().astimezone().isoformat(),
        }
        (output_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"结果目录：{output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
