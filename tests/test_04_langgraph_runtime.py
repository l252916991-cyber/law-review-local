import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from tests.support import IsolatedDatabaseTestCase
from pathlib import Path
from unittest.mock import patch


TEST_DATA = tempfile.mkdtemp(prefix="lexvault-langgraph-tests-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA
os.environ["LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB"] = str(Path(TEST_DATA) / "checkpoints.sqlite")

from fastapi.testclient import TestClient  # noqa: E402

from app.agents import EvidenceAgent, FactAgent, create_coordinator, get_run_trace  # noqa: E402
from app.db import SCHEMA, connect, init_db, transaction  # noqa: E402
from app.langgraph_agents import (  # noqa: E402
    LangGraphRunError,
    answer_contract,
    create_langgraph_coordinator,
)
from app.main import app  # noqa: E402
from app.runtime_comparison import compare_runtime_results  # noqa: E402


class LangGraphRuntimeTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def coordinator(self, **kwargs):
        return create_langgraph_coordinator(
            1,
            prefer_remote_embeddings=False,
            checkpoint_path=Path(TEST_DATA) / "checkpoints.sqlite",
            **kwargs,
        )

    def test_native_and_langgraph_are_structurally_equivalent(self):
        for question in (
            "梳理募集资金的主要流向。",
            "募集资金总额和投资人数是多少？",
            "张某关于固定回报审批的陈述是否矛盾？",
            "检查本案证据疏漏和待补证事项。",
        ):
            with self.subTest(question=question):
                native = create_coordinator(1, False).process_query(
                    question, "等价测试", False, persist_memory=False
                )
                graph = self.coordinator().process_query(
                    question, "等价测试", False, persist_memory=False
                )
                comparison = compare_runtime_results(native, graph)
                self.assertTrue(comparison["equivalent"], comparison)
                self.assertEqual(graph["runtime"], "langgraph")
                self.assertGreater(graph["checkpoint_size_bytes"], 0)
                normalize = lambda metrics: {k: v for k, v in metrics.items() if k != "latency_ms"}
                self.assertEqual(normalize(native["retrieval_metrics"]), normalize(graph["retrieval_metrics"]))

    def test_optional_nodes_and_gap_dependencies_are_real(self):
        ordinary = self.coordinator().process_query(
            "梳理募集资金流向", "依赖测试", False, persist_memory=False
        )
        self.assertNotIn("contradiction", {step["node"] for step in ordinary["steps"]})
        self.assertNotIn("gap_detection", {step["node"] for step in ordinary["steps"]})

        gap = self.coordinator().process_query(
            "检查本案证据疏漏和待补证事项", "依赖测试", False, persist_memory=False
        )
        trace = get_run_trace(gap["run_id"])
        gap_step = next(step for step in trace["steps"] if step["node_name"] == "gap_detection")
        self.assertEqual(gap_step["input"]["dependencies"], ["evidence", "facts"])

    def test_facts_and_evidence_execute_in_parallel(self):
        barrier = threading.Barrier(2)

        def fact_run(_self, question, contexts):
            barrier.wait(timeout=2)
            time.sleep(0.03)
            return {"summary": "facts parallel", "facts": []}

        def evidence_run(_self, question, contexts):
            barrier.wait(timeout=2)
            time.sleep(0.03)
            return {"summary": "evidence parallel", "sources": []}

        with patch.object(FactAgent, "run", fact_run), patch.object(
            EvidenceAgent, "run", evidence_run
        ):
            result = self.coordinator().process_query(
                "梳理募集资金流向", "并行测试", False, persist_memory=False
            )
        names = {step["node"] for step in result["steps"]}
        self.assertTrue({"facts", "evidence"}.issubset(names))

    def test_resume_does_not_repeat_critic_or_side_effects(self):
        failed_once = {"value": False}

        def inject(node, state):
            if node == "memory" and not failed_once["value"]:
                failed_once["value"] = True
                raise RuntimeError("测试注入：Memory 首次失败")

        answer = "结论依据[资料1]，仍需律师复核。"
        coordinator = self.coordinator(failure_injector=inject)
        with patch("app.agents.call_local_llm", return_value=answer) as llm:
            with self.assertRaises(LangGraphRunError) as caught:
                coordinator.process_query("梳理募集资金流向", "恢复测试", True)
            run_id = caught.exception.run_id
            resumed_nodes = []
            result = self.coordinator(
                failure_injector=lambda node, state: resumed_nodes.append(node)
            ).resume(run_id)

        self.assertEqual(llm.call_count, 1)
        self.assertEqual(resumed_nodes, ["memory"])
        self.assertEqual(result["resume_count"], 1)
        with closing(connect()) as conn, conn:
            memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_run_id=?", (run_id,)
            ).fetchone()[0]
            duplicate_steps = conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT node_name FROM agent_steps WHERE run_id=?
                       GROUP BY node_name HAVING COUNT(*) > 1
                   )""",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(memories, 1)
        self.assertEqual(duplicate_steps, 0)

    def test_resume_after_memory_side_effect_is_idempotent(self):
        coordinator = self.coordinator()
        remember_once = coordinator._remember_once
        failed = False

        def write_then_fail(state):
            nonlocal failed
            memory_id = remember_once(state)
            if not failed:
                failed = True
                raise RuntimeError("记忆已提交，但节点尚未 checkpoint")
            return memory_id

        with patch.object(coordinator, "_remember_once", side_effect=write_then_fail):
            with self.assertRaises(LangGraphRunError) as caught:
                coordinator.process_query("检查本案证据疏漏", "幂等测试", False)
        run_id = caught.exception.run_id
        result = self.coordinator().resume(run_id)
        with transaction() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_run_id=?", (run_id,)
            ).fetchone()[0], 1)
            rows = conn.execute(
                "SELECT gap_type, description FROM gap_detections WHERE run_id=?", (run_id,)
            ).fetchall()
        self.assertEqual(len(rows), len({tuple(row) for row in rows}))
        self.assertFalse(result["resumable"])

    def test_empty_gap_output_is_supported(self):
        with patch("app.langgraph_agents.GapDetectionAgent.run", return_value={"gaps": [], "summary": "无疏漏"}):
            result = self.coordinator().process_query("检查本案证据疏漏", use_llm=False)
        self.assertIn("gap_detection", {step["node"] for step in result["steps"]})

    def test_answer_contract_normalizes_citation_spacing(self):
        self.assertTrue(all(answer_contract("根据[资料 1]，需律师复核", 1).values()))
        self.assertFalse(answer_contract("根据[资料 2]，需律师复核", 1)["citations_valid"])

    def test_restart_keeps_checkpoint_and_run_is_resumable(self):
        def fail_memory(node, state):
            if node == "memory":
                raise RuntimeError("模拟服务中断")

        with self.assertRaises(LangGraphRunError) as caught:
            self.coordinator(failure_injector=fail_memory).process_query("梳理资金流向", use_llm=False)
        run_id = caught.exception.run_id
        with transaction() as conn:
            conn.execute("UPDATE agent_runs SET status='running' WHERE id=?", (run_id,))
        init_db(seed=False, recover_runs=True)
        trace = get_run_trace(run_id)
        self.assertEqual(trace["status"], "failed")
        self.assertTrue(trace["resumable"])
        self.assertEqual(self.coordinator().resume(run_id)["resume_count"], 1)

    def test_old_database_migration_preserves_native_history(self):
        path = Path(TEST_DATA) / "old-schema.sqlite"
        old_schema = SCHEMA
        for column in (
            "    runtime TEXT NOT NULL DEFAULT 'native',\n",
            "    checkpoint_thread_id TEXT NOT NULL DEFAULT '',\n",
            "    resume_count INTEGER NOT NULL DEFAULT 0,\n",
        ):
            old_schema = old_schema.replace(column, "")
        conn = sqlite3.connect(path)
        try:
            conn.executescript(old_schema)
            conn.execute("INSERT INTO cases(title, created_at, updated_at) VALUES ('历史案件', 'old', 'old')")
            conn.execute("INSERT INTO agent_runs(case_id, question, route, status, final_answer, created_at) VALUES (1, '历史问题', '事实', 'completed', '保留原答案', 'old')")
            conn.commit()
        finally:
            conn.close()
        with patch("app.db.DB_PATH", path):
            init_db(seed=False)
            init_db(seed=False)
            with transaction() as conn:
                row = conn.execute("SELECT * FROM agent_runs WHERE id=1").fetchone()
        self.assertEqual(row["runtime"], "native")
        self.assertEqual(row["resume_count"], 0)
        self.assertEqual(row["final_answer"], "保留原答案")

    def test_empty_retrieval_and_model_fallback_are_supported(self):
        with patch("app.langgraph_agents.RetrievalAgent.retrieve", return_value={"contexts": [], "metrics": {}}), patch(
            "app.agents.call_local_llm", side_effect=RuntimeError("模型不可用")
        ):
            result = self.coordinator().process_query(
                "完全不存在的材料", "异常测试", True, persist_memory=False
            )
        self.assertFalse(result["llm_used"])
        self.assertIn("模型不可用", result["fallback_reason"])
        self.assertEqual(result["citations"], [])


class LangGraphAPITest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(app)
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def test_both_modes_and_invalid_mode(self):
        payload = {
            "question": "梳理募集资金流向",
            "use_llm": False,
            "use_remote_embeddings": False,
        }
        native = self.client.post("/api/cases/1/agent-chat", json=payload)
        graph = self.client.post(
            "/api/cases/1/agent-chat", json={**payload, "mode": "langgraph"}
        )
        invalid = self.client.post(
            "/api/cases/1/agent-chat", json={**payload, "mode": "unknown"}
        )
        self.assertEqual(native.status_code, 200)
        self.assertEqual(native.json()["runtime"], "native")
        self.assertEqual(graph.status_code, 200)
        self.assertEqual(graph.json()["runtime"], "langgraph")
        self.assertEqual(invalid.status_code, 422)

    def test_compare_endpoint(self):
        snapshot = [{"id": 42, "content": "执行前记忆"}]
        with patch("app.rag.recall_memories", return_value=snapshot) as recall:
            response = self.client.post(
                "/api/cases/1/agent-compare",
                json={
                    "question": "检查本案证据疏漏和待补证事项",
                    "use_llm": False,
                    "use_remote_embeddings": False,
                },
            )
        self.assertEqual(recall.call_count, 1)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["comparison"]["equivalent"], payload["comparison"])
        self.assertEqual(payload["native"]["memory_hits"], snapshot)
        self.assertEqual(payload["langgraph"]["memory_hits"], snapshot)
        self.assertFalse(payload["native"]["steps"][-1]["summary"].startswith("最终结论已写入"))
        self.assertFalse(payload["langgraph"]["steps"][-1]["summary"].startswith("最终结论已写入"))

    def test_resume_error_contracts(self):
        self.assertEqual(self.client.post("/api/agent-runs/999999/resume").status_code, 404)
        native = create_coordinator(1, False).process_query(
            "梳理募集资金流向", "API恢复测试", False, persist_memory=False
        )
        self.assertEqual(
            self.client.post(f"/api/agent-runs/{native['run_id']}/resume").status_code,
            409,
        )
        graph = create_langgraph_coordinator(
            1, False, checkpoint_path=Path(TEST_DATA) / "checkpoints.sqlite"
        ).process_query("梳理募集资金流向", "API恢复测试", False, persist_memory=False)
        self.assertEqual(
            self.client.post(f"/api/agent-runs/{graph['run_id']}/resume").status_code,
            409,
        )
        with transaction() as conn:
            conn.execute("UPDATE agent_runs SET status='running' WHERE id=?", (graph["run_id"],))
        self.assertEqual(self.client.post(f"/api/agent-runs/{graph['run_id']}/resume").status_code, 409)
        with transaction() as conn:
            conn.execute(
                "UPDATE agent_runs SET status='failed', checkpoint_thread_id='missing-thread' WHERE id=?",
                (graph["run_id"],),
            )
        self.assertEqual(self.client.post(f"/api/agent-runs/{graph['run_id']}/resume").status_code, 409)
        self.assertFalse(get_run_trace(graph["run_id"])["resumable"])

    def test_api_resume_success_after_checkpoint_reopen(self):
        def inject(node, state):
            if node == "memory":
                raise RuntimeError("API恢复测试")

        coordinator = create_langgraph_coordinator(1, False, failure_injector=inject)
        with patch("app.langgraph_agents.create_langgraph_coordinator", return_value=coordinator):
            failure = self.client.post("/api/cases/1/agent-chat", json={
                "question": "梳理募集资金流向", "mode": "langgraph", "use_llm": False,
            })
        self.assertEqual(failure.status_code, 500)
        detail = failure.json()["detail"]
        run_id = detail["run_id"]
        self.assertEqual(detail["checkpoint_thread_id"], f"agent-run-{run_id}")
        self.assertTrue(detail["resumable"])
        self.assertTrue(get_run_trace(run_id)["resumable"])
        recent = self.client.get("/api/cases/1/platform-metrics").json()["recent_runs"]
        self.assertTrue(next(run for run in recent if run["id"] == run_id)["resumable"])
        response = self.client.post(f"/api/agent-runs/{run_id}/resume")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["run_id"], run_id)
        self.assertEqual(response.json()["resume_count"], 1)
        self.assertEqual(response.json()["mode"], "langgraph")
        self.assertFalse(response.json()["resumable"])


if __name__ == "__main__":
    unittest.main()
