"""Small, reproducible RAG benchmark for interview and regression use."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .db import connect, now, transaction
from .rag import HybridRetriever
from .services import rowdict


DEMO_GROUND_TRUTH = [
    {
        "query": "张某是否批准了年化12%的固定回报宣传？",
        "expected": [("02_张某询问笔录.txt", 3), ("05_电子邮件与宣传材料.txt", 1)],
    },
    {
        "query": "募集资金总额和投资人数是多少？",
        "expected": [("01_起诉意见书.txt", 2), ("04_账户流水摘要.txt", 1)],
    },
    {
        "query": "张某关于不知情的辩解有哪些材料反驳？",
        "expected": [("02_张某询问笔录.txt", 1), ("03_李某询问笔录.txt", 1)],
    },
    {
        "query": "涉案资金流向张某个人账户的记录",
        "expected": [("04_账户流水摘要.txt", 2), ("04_账户流水摘要.txt", 3)],
    },
]


def _case_ground_truth(case_id: int, supplied: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], str]:
    conn = connect()
    try:
        case = conn.execute("SELECT title, case_no FROM cases WHERE id=?", (case_id,)).fetchone()
        available = {(row["name"], row["page_no"]) for row in conn.execute(
            "SELECT d.name, p.page_no FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.case_id=?",
            (case_id,),
        )}
    finally:
        conn.close()
    if case is None:
        raise ValueError("案件不存在")
    if supplied is None:
        if (case["case_no"] != "（2026）演刑初字第008号"
                or case["title"] != "某科技公司涉嫌非法吸收公众存款案（演示）"):
            raise ValueError("非演示案件必须提供本案 ground_truth，不能使用演示案件标准答案")
        supplied, dataset = DEMO_GROUND_TRUTH, "demo-legal-rag-v1"
    else:
        dataset = "case-specific-ground-truth"
    if not isinstance(supplied, list) or not supplied:
        raise ValueError("ground_truth 必须包含至少一条查询")
    normalized = []
    for item in supplied:
        if not isinstance(item, dict) or not isinstance(item.get("query"), str) or not item["query"].strip():
            raise ValueError("每条标准答案必须包含非空 query")
        if not isinstance(item.get("expected"), (list, tuple)):
            raise ValueError("expected 必须为本案文档名称和页码列表；无答案题使用空列表")
        expected = []
        for value in item["expected"]:
            if isinstance(value, dict):
                value = (value.get("document"), value.get("page"))
            if (not isinstance(value, (list, tuple)) or len(value) != 2
                    or not isinstance(value[0], str) or type(value[1]) is not int or value[1] < 1):
                raise ValueError("expected 来源必须为 [文档名, 正整数页码]")
            pair = (value[0], value[1])
            if pair not in available:
                raise ValueError("ground_truth 包含不属于本案或不存在的文档页面")
            expected.append(pair)
        normalized.append({"query": item["query"].strip(), "expected": sorted(set(expected))})
    return normalized, dataset


def evaluate_case(
    case_id: int, prefer_remote_embeddings: bool = True, k: int = 5, *,
    ground_truth: list[dict[str, Any]] | None = None, persist: bool = True,
) -> dict[str, Any]:
    if type(k) is not int or not 1 <= k <= 100:
        raise ValueError("k 必须在 1 到 100 之间")
    truth, dataset = _case_ground_truth(case_id, ground_truth)
    evaluation_id = uuid.uuid4().hex
    retriever = HybridRetriever(case_id, prefer_remote_embeddings)
    cases = []
    for item in truth:
        started = time.perf_counter()
        results, retrieval_metrics = retriever.retrieve(item["query"], k)
        latency_ms = round((time.perf_counter() - started) * 1000)
        returned = [(result["name"], result["page_no"]) for result in results]
        expected = set(item["expected"])
        hits = [rank for rank, pair in enumerate(returned, 1) if pair in expected]
        recall = len(set(returned) & expected) / len(expected) if expected else None
        mrr = 1 / hits[0] if hits else 0.0
        quote_presence_rate = sum(bool(result.get("quote")) for result in results) / len(results) if results else 0.0
        cases.append(
            {
                "query": item["query"],
                "expected": item["expected"],
                "returned": returned,
                "recall_at_k": round(recall, 4) if recall is not None else None,
                "mrr": round(mrr, 4),
                "citation_coverage": round(quote_presence_rate, 4),  # deprecated alias
                "quote_presence_rate": round(quote_presence_rate, 4),
                "answer_citation_faithfulness": None,
                "answerable": bool(expected),
                "unanswerable_correctly_empty": not results if not expected else None,
                "latency_ms": latency_ms,
                "retrieval": retrieval_metrics,
            }
        )
    if persist:
        # Publish one complete evaluation batch atomically, never a partial
        # prefix that the dashboard could mistake for a finished evaluation.
        with transaction() as conn:
            conn.executemany(
                """INSERT INTO rag_evaluations(
                    case_id, query, expected_json, returned_json, recall_at_k, mrr,
                    citation_coverage, latency_ms, created_at, evaluation_id, dataset_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(
                    case_id, item["query"], json.dumps(item["expected"], ensure_ascii=False),
                    json.dumps(item["returned"], ensure_ascii=False), item["recall_at_k"] or 0.0,
                    item["mrr"], item["quote_presence_rate"], item["latency_ms"], now(),
                    evaluation_id, dataset,
                ) for item in cases],
            )
    count = len(cases) or 1
    answerable = [item for item in cases if item["answerable"]]
    unanswerable = [item for item in cases if not item["answerable"]]
    return {
        "dataset": dataset,
        "evaluation_id": evaluation_id,
        "scope": "page_retrieval_only",
        "queries": len(cases),
        "k": k,
        "recall_at_k": round(sum(item["recall_at_k"] for item in answerable) / len(answerable), 4) if answerable else None,
        "mrr": round(sum(item["mrr"] for item in answerable) / len(answerable), 4) if answerable else None,
        "citation_coverage": round(sum(item["citation_coverage"] for item in cases) / count, 4),
        "quote_presence_rate": round(sum(item["quote_presence_rate"] for item in cases) / count, 4),
        "answer_citation_faithfulness": None,
        "metric_notes": {"citation_coverage": "deprecated alias of quote_presence_rate; not answer grounding or entailment"},
        "answerable_queries": len(answerable),
        "unanswerable_queries": len(unanswerable),
        "unanswerable_empty_rate": round(sum(item["unanswerable_correctly_empty"] for item in unanswerable) / len(unanswerable), 4) if unanswerable else None,
        "average_latency_ms": round(sum(item["latency_ms"] for item in cases) / count),
        "cases": cases,
    }


def platform_metrics(case_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        runs = rowdict(
            conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0) AS completed,
                       COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0) AS failed,
                       COALESCE(AVG(CASE WHEN status='completed' THEN total_ms END), 0) AS avg_ms
                FROM agent_runs WHERE case_id = ?
                """,
                (case_id,),
            ).fetchone()
        )
        vector_spaces = [rowdict(row) for row in conn.execute(
                """
                SELECT COUNT(*) AS pages, dimensions, backend, model
                FROM embedding_cache e JOIN pages p ON p.id=e.page_id
                JOIN documents d ON d.id=p.document_id WHERE d.case_id=?
                GROUP BY model, backend, dimensions ORDER BY model, backend, dimensions
                """,
                (case_id,),
            )]
        vectors = {
            "pages": sum(space["pages"] for space in vector_spaces),
            "dimensions": vector_spaces[0]["dimensions"] if len(vector_spaces) == 1 else 0,
            "backend": vector_spaces[0]["backend"] if len(vector_spaces) == 1 else "mixed" if vector_spaces else "not-built",
            "spaces": vector_spaces,
        }
        memory_count = conn.execute("SELECT COUNT(*) FROM memories WHERE case_id=?", (case_id,)).fetchone()[0]
        latest_batch = conn.execute(
            "SELECT evaluation_id FROM rag_evaluations WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)
        ).fetchone()
        latest_rows = conn.execute(
            "SELECT * FROM rag_evaluations WHERE case_id=? AND evaluation_id=? ORDER BY id",
            (case_id, latest_batch["evaluation_id"]),
        ).fetchall() if latest_batch and latest_batch["evaluation_id"] else []
        latest = [rowdict(row) for row in latest_rows]
        recent_runs = [
            rowdict(row)
            for row in conn.execute(
                """SELECT id, question, route, status, total_ms, created_at,
                          runtime, checkpoint_thread_id, resume_count
                   FROM agent_runs WHERE case_id=? ORDER BY id DESC LIMIT 8""",
                (case_id,),
            )
        ]
    finally:
        conn.close()
    for run in recent_runs:
        run["resumable"] = False
        if run["runtime"] == "langgraph" and run["status"] == "failed":
            from .langgraph_agents import LangGraphCoordinator

            run["resumable"] = LangGraphCoordinator(case_id).checkpoint_available(run["checkpoint_thread_id"])
    evaluation = None
    if latest:
        answerable = [row for row in latest if json.loads(row["expected_json"])]
        unanswerable = [row for row in latest if not json.loads(row["expected_json"])]
        evaluation = {
            "evaluation_id": latest[0]["evaluation_id"],
            "dataset": latest[0]["dataset_name"],
            "scope": "page_retrieval_only",
            "queries": len(latest),
            "recall_at_k": round(sum(row["recall_at_k"] for row in answerable) / len(answerable), 4) if answerable else None,
            "mrr": round(sum(row["mrr"] for row in answerable) / len(answerable), 4) if answerable else None,
            "citation_coverage": round(sum(row["citation_coverage"] for row in latest) / len(latest), 4),
            "quote_presence_rate": round(sum(row["citation_coverage"] for row in latest) / len(latest), 4),
            "answer_citation_faithfulness": None,
            "unanswerable_empty_rate": round(sum(not json.loads(row["returned_json"]) for row in unanswerable) / len(unanswerable), 4) if unanswerable else None,
            "average_latency_ms": round(sum(row["latency_ms"] for row in latest) / len(latest)),
        }
    return {
        "agent_runs": runs,
        "vector_index": vectors,
        "memory_count": memory_count,
        "evaluation": evaluation,
        "legacy_evaluation_unverifiable": bool(latest_batch and not latest_batch["evaluation_id"]),
        "recent_runs": recent_runs,
    }
