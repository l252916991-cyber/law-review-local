import os
import tempfile
import unittest
from tests.support import IsolatedDatabaseTestCase
from unittest.mock import patch


TEST_DATA = tempfile.mkdtemp(prefix="lexvault-agent-tests-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA

from app.agents import (  # noqa: E402
    ContradictionAgent,
    CriticAgent,
    PlannerAgent,
    RetrievalAgent,
    create_coordinator,
    get_run_trace,
)
from app.db import connect, init_db  # noqa: E402
from app.evaluation import evaluate_case, platform_metrics  # noqa: E402
from app.lawbench import extract_choice, lawbench_summary, sample_questions, validate_lawbench  # noqa: E402
from app.rag import HybridRetriever, expand_retrieval_query, hashed_embedding, recall_memories  # noqa: E402


class HybridRAGTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def test_hashed_embedding_is_deterministic(self):
        first = hashed_embedding("固定回报审批")
        second = hashed_embedding("固定回报审批")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 384)

    def test_legal_query_expansion_is_domain_grounded(self):
        expanded = expand_retrieval_query("不知情辩解如何反驳")
        self.assertIn("客观记录", expanded)
        self.assertIn("电子邮件", expanded)

    def test_vector_index_is_persistent_and_incremental(self):
        retriever = HybridRetriever(1, prefer_remote_embeddings=False)
        first = retriever.ensure_vector_index(force=True)
        second = retriever.ensure_vector_index()
        self.assertGreaterEqual(first["embedded"], 10)
        self.assertEqual(second["embedded"], 0)
        self.assertEqual(second["cached"], first["pages"])

    def test_hybrid_retrieval_exposes_rrf_evidence(self):
        retriever = HybridRetriever(1, prefer_remote_embeddings=False)
        results, metrics = retriever.retrieve("张某固定回报审批是否矛盾", 5)
        self.assertTrue(results)
        self.assertEqual(metrics["mode"], "FTS5/BM25 + Legal Lexical + Vector + RRF")
        self.assertTrue(any("电子邮件" in item["name"] for item in results))
        self.assertTrue(all("retrieval_explain" in item for item in results))
        self.assertTrue(any(len(item["channels"]) == 2 for item in results))

    def test_keyword_and_vector_routes_are_distinct(self):
        agent = RetrievalAgent(1, prefer_remote_embeddings=False)
        keyword = agent.retrieve_evidence("张某", "keyword")
        semantic = agent.retrieve_evidence("负责人声称自己不知情", "semantic")
        self.assertIn("证据", keyword)
        self.assertIn("证据", semantic)
        self.assertNotEqual(keyword, semantic)


class MultiAgentRuntimeTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def test_planner_builds_dependency_graph(self):
        route, plan = PlannerAgent().build_plan("张某和李某的陈述是否矛盾？")
        nodes = {node.name: node for node in plan}
        self.assertEqual(route, "多文档对比")
        self.assertIn("contradiction", nodes)
        self.assertEqual(nodes["facts"].depends_on, ("retrieve",))
        self.assertIn("contradiction", nodes["critic"].depends_on)
        self.assertEqual(nodes["memory"].depends_on, ("critic",))

    def test_contradiction_agent_returns_grounded_pairs(self):
        contexts, _ = HybridRetriever(1, False).retrieve("张某固定回报审批矛盾", 6)
        result = ContradictionAgent().run("固定回报是否矛盾", contexts)
        self.assertIn("summary", result)
        self.assertTrue(result["conflicts"])
        self.assertIn("资料", result["conflicts"][0]["denial"])

    def test_end_to_end_run_persists_trace_and_memory(self):
        coordinator = create_coordinator(1, prefer_remote_embeddings=False)
        result = coordinator.process_query(
            "张某关于固定回报审批的陈述是否矛盾？", "测试律师", use_llm=False
        )
        self.assertEqual(result["agent_type"], "DAG Multi-Agent")
        self.assertEqual(result["route"], "多文档对比")
        self.assertGreaterEqual(len(result["steps"]), 8)
        self.assertEqual(len(result["citations"]), 6)
        self.assertTrue(all(item["retrieval_explain"] for item in result["citations"]))

        trace = get_run_trace(result["run_id"])
        self.assertEqual(trace["status"], "completed")
        self.assertEqual(len(trace["steps"]), len(result["steps"]))
        self.assertTrue(any(step["node_name"] == "critic" for step in trace["steps"]))

        memories = recall_memories(1, "固定回报审批", 3, prefer_remote_embeddings=False)
        self.assertTrue(memories)
        self.assertEqual(memories[0]["source_run_id"], result["run_id"])

    def test_specialist_steps_are_independently_observable(self):
        result = create_coordinator(1, False).process_query("梳理募集资金流向", "测试律师", False)
        trace = get_run_trace(result["run_id"])
        names = {step["node_name"] for step in trace["steps"]}
        self.assertTrue({"planner", "retrieve", "facts", "evidence", "critic", "memory"}.issubset(names))
        self.assertTrue(all(step["latency_ms"] >= 0 for step in trace["steps"]))

    def test_critic_uses_full_mode_and_exposes_fallback_reason(self):
        critic = CriticAgent()
        self.assertEqual(critic.model, "Qwythos-9B-v2-4bit-mlx")
        self.assertEqual(critic.timeout, 180)
        specialists = {"facts": {"facts": [], "summary": "事实不足"}, "evidence": {"summary": "证据不足"}}
        with patch("app.agents.call_local_llm", side_effect=RuntimeError("模拟 180 秒总时限")):
            result = critic.run("测试问题", "语义检索", [], specialists, True)
        self.assertFalse(result["llm_used"])
        self.assertEqual(result["llm_model"], "Qwythos-9B-v2-4bit-mlx")
        self.assertIn("180 秒", result["fallback_reason"])


class EvaluationTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def test_offline_rag_evaluation_has_real_metrics(self):
        report = evaluate_case(1, prefer_remote_embeddings=False, k=5)
        self.assertEqual(report["queries"], 4)
        self.assertGreaterEqual(report["recall_at_k"], 0.5)
        self.assertGreater(report["mrr"], 0)
        self.assertEqual(report["citation_coverage"], 1.0)

    def test_platform_metrics_aggregate_runs_vectors_and_eval(self):
        create_coordinator(1, False).process_query("梳理资金流向", "指标测试", False)
        metrics = platform_metrics(1)
        self.assertGreaterEqual(metrics["agent_runs"]["completed"], 1)
        self.assertGreaterEqual(metrics["vector_index"]["pages"], 10)
        self.assertGreaterEqual(metrics["memory_count"], 1)
        self.assertIsNotNone(metrics["evaluation"])

    def test_schema_contains_interview_grade_runtime_tables(self):
        conn = connect()
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
        finally:
            conn.close()
        for name in ("pages_fts", "embedding_cache", "agent_runs", "agent_steps", "memories", "rag_evaluations"):
            self.assertIn(name, names)

    def test_lawbench_loader_validates_twenty_task_counts(self):
        report = validate_lawbench()
        self.assertTrue(report["valid"])
        self.assertEqual(report["total_tasks"], 20)
        self.assertEqual(report["total_questions"], 10000)
        self.assertTrue(all(count == 500 for count in report["task_counts"].values()))

    def test_lawbench_sampling_is_reproducible_and_hides_answers(self):
        first = sample_questions(limit=8, seed=2026)
        second = sample_questions(limit=8, seed=2026)
        self.assertEqual(first, second)
        self.assertTrue(all("answer" not in item for item in first))
        self.assertGreaterEqual(len({item["task_id"] for item in first}), 3)
        self.assertEqual(lawbench_summary(limit=0)["examples"], [])

    def test_lawbench_choice_parser_supports_reference_and_model_formats(self):
        self.assertEqual(extract_choice("正确答案：B。"), "B")
        self.assertEqual(extract_choice("[正确答案]C<eoa>"), "C")
        self.assertIsNone(extract_choice("无法判断"))


if __name__ == "__main__":
    unittest.main()
