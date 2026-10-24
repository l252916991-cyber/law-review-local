"""M04: shared multi-turn context for native Agent, LangGraph, jobs and resume."""
import json
import os
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.multiturn_fixtures import SCENARIOS
from tests.support import IsolatedDatabaseTestCase
from tests.test_multiturn_chat import append_turn, citation, create_case_with_documents, make_conversation


class NativeAgentContextTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.db import init_db

        init_db(seed=False)

    def setUp(self):
        self.case_id, self.ids = create_case_with_documents(SCENARIOS["pair"])
        self.conversation_id = make_conversation(self.case_id)
        append_turn(
            self.conversation_id, "甲公司采购合同的签署方有哪些？", "甲公司与丙公司[资料1]。请律师复核原文与上述结论。",
            citations=[citation(self.ids["jia_contract"], "甲公司采购合同.txt", 1)],
        )

    def test_agent_uses_resolved_object_and_persists_run_snapshot(self):
        from app.agents import create_coordinator
        from app.context import prepare_turn
        from app.db import connect

        context = prepare_turn(
            self.case_id, "它的付款期限呢？", self.conversation_id,
            user_name="测试律师", use_llm=False,
        )
        with patch("app.agents.recall_memories", return_value=[]):
            result = create_coordinator(self.case_id, False).process_query(
                "它的付款期限呢？", "测试律师", False, persist_memory=False, context=context,
            )
        self.assertEqual(result["answer_status"], "answered")
        self.assertEqual(result["context"]["available"], True)
        self.assertEqual(result["context"]["conversation_id"], self.conversation_id)
        self.assertEqual(result["context"]["resolution"]["status"], "resolved")
        self.assertEqual(result["context"]["resolution"]["object_document_ids"], [self.ids["jia_contract"]])
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT conversation_id, context_json FROM agent_runs WHERE id=?", (result["run_id"],)
            ).fetchone()
        identity = json.loads(row["context_json"])
        self.assertEqual(row["conversation_id"], self.conversation_id)
        self.assertEqual(identity["resolution"]["object_document_ids"], [self.ids["jia_contract"]])
        self.assertEqual(identity["history"]["digest"], result["context"]["history"]["digest"])

    def test_agent_passes_history_and_object_note_to_critic_only(self):
        from app.agents import create_coordinator
        from app.context import prepare_turn

        context = prepare_turn(
            self.case_id, "它的付款期限呢？", self.conversation_id,
            user_name="测试律师", use_llm=True,
        )
        answer = "付款期限为验收合格后60日内[资料1]。请律师复核原文与上述结论。"
        with patch("app.agents.recall_memories", return_value=[]), \
                patch("app.agents.call_local_llm", return_value=answer) as model:
            result = create_coordinator(self.case_id, False).process_query(
                "它的付款期限呢？", "测试律师", True, persist_memory=False, context=context,
            )
        self.assertTrue(result["llm_used"])
        kwargs = model.call_args.kwargs
        self.assertIn("【历史对话", kwargs["history"])
        self.assertIn("甲公司与丙公司", kwargs["history"])
        self.assertIn("《甲公司采购合同.txt》", kwargs["object_note"])
        # Only this round's retrieval can become citations.
        self.assertTrue(result["citations"])
        self.assertTrue(all(item["document_id"] == self.ids["jia_contract"] for item in result["citations"]))
        self.assertTrue(model.call_args.args[0].startswith("它的付款期限呢？"))

    def test_agent_refuses_to_run_when_clarification_is_needed(self):
        from app.agents import create_coordinator
        from app.context import prepare_turn
        from app.db import connect

        fresh = make_conversation(self.case_id)
        context = prepare_turn(self.case_id, "它的付款期限呢？", fresh)
        with closing(connect()) as conn:
            before = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "澄清"):
            create_coordinator(self.case_id, False).process_query(
                "它的付款期限呢？", "测试律师", False, persist_memory=False, context=context,
            )
        with closing(connect()) as conn:
            after = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        self.assertEqual(after, before)


class AgentApiTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app.db import init_db
        from app.main import app

        init_db(seed=False)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def setUp(self):
        self.case_id, self.ids = create_case_with_documents(SCENARIOS["pair"])
        self.conversation_id = make_conversation(self.case_id)
        append_turn(
            self.conversation_id, "甲公司采购合同的签署方有哪些？", "甲公司与丙公司[资料1]。请律师复核原文与上述结论。",
            citations=[citation(self.ids["jia_contract"], "甲公司采购合同.txt", 1)],
        )

    def agent(self, question, conversation_id, mode="multi_agent", use_llm=False):
        return self.client.post(
            f"/api/cases/{self.case_id}/agent-chat",
            json={
                "question": question,
                "conversation_id": conversation_id,
                "mode": mode,
                "use_llm": use_llm,
                "use_remote_embeddings": False,
            },
        )

    def messages(self, conversation_id):
        from app.db import connect

        with closing(connect()) as conn:
            return conn.execute(
                "SELECT role, kind, source_run_id FROM messages WHERE conversation_id=? ORDER BY id",
                (conversation_id,),
            ).fetchall()

    def test_agent_follow_up_binds_conversation_and_records_turn(self):
        from app.db import connect

        native = self.agent("它的付款期限呢？", self.conversation_id)
        self.assertEqual(native.status_code, 200, native.text)
        payload = native.json()
        self.assertEqual(payload["conversation_id"], self.conversation_id)
        self.assertEqual(payload["answer_status"], "answered")
        self.assertEqual(payload["context"]["resolution"]["object_document_ids"], [self.ids["jia_contract"]])
        self.assertTrue(payload["citations"])
        self.assertTrue(all(item["document_id"] == self.ids["jia_contract"] for item in payload["citations"]))
        graph = self.agent("它的总价是多少？", self.conversation_id, mode="langgraph")
        self.assertEqual(graph.status_code, 200, graph.text)
        self.assertEqual(graph.json()["conversation_id"], self.conversation_id)
        self.assertEqual(graph.json()["context"]["resolution"]["status"], "resolved")
        rows = self.messages(self.conversation_id)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["source_run_id"] for row in rows[2:] if row["role"] == "assistant"))
        with closing(connect()) as conn:
            runs = conn.execute(
                "SELECT context_json FROM agent_runs WHERE conversation_id=? ORDER BY id", (self.conversation_id,)
            ).fetchall()
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(json.loads(row["context_json"])["question"] for row in runs))

    def test_unknown_and_archived_conversations_are_rejected_before_run(self):
        from app.db import connect

        with closing(connect()) as conn:
            before = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        unknown = self.agent("它的付款期限呢？", 999999)
        self.assertEqual(unknown.status_code, 404, unknown.text)
        archived = make_conversation(self.case_id)
        from app.db import now, transaction

        with transaction() as conn:
            conn.execute(
                "UPDATE conversations SET archived_at=?, archive_expires_at=? WHERE id=?",
                (now(), 2**62, archived),
            )
        rejected = self.agent("它的付款期限呢？", archived)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        with closing(connect()) as conn:
            after = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        self.assertEqual(after, before)

    def test_ambiguous_agent_follow_up_returns_clarification_without_run(self):
        from app.db import connect

        first = self.client.post(
            f"/api/cases/{self.case_id}/chat",
            json={"question": "两份采购合同的付款期限分别是什么？", "use_llm": False, "use_remote_embeddings": False},
        )
        self.assertEqual(first.status_code, 200, first.text)
        conversation_id = first.json()["conversation_id"]
        with closing(connect()) as conn:
            before = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        response = self.agent("它的付款期限呢？", conversation_id)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["answer_status"], "clarification")
        self.assertIsNone(payload["run_id"])
        self.assertEqual(payload["citations"], [])
        self.assertNotIn("[资料", payload["answer"])
        self.assertEqual(payload["conversation_id"], conversation_id)
        with closing(connect()) as conn:
            after = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        self.assertEqual(after, before)
        last = self.messages(conversation_id)[-1]
        self.assertEqual((last["role"], last["kind"]), ("assistant", "clarification"))


class ResumeContextTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app.db import init_db
        from app.main import app

        init_db(seed=False)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def setUp(self):
        self.case_id, self.ids = create_case_with_documents(SCENARIOS["pair"])
        self.conversation_id = make_conversation(self.case_id)
        append_turn(
            self.conversation_id, "甲公司采购合同的签署方有哪些？", "甲公司与丙公司[资料1]。请律师复核原文与上述结论。",
            citations=[citation(self.ids["jia_contract"], "甲公司采购合同.txt", 1)],
        )

    def fail_run(self, conversation_id):
        from app.langgraph_agents import create_langgraph_coordinator

        def inject(node, state):
            if node == "memory":
                raise RuntimeError("恢复测试注入失败")

        coordinator = create_langgraph_coordinator(
            self.case_id, False, failure_injector=inject,
            checkpoint_path=Path(os.environ["LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB"]),
        )
        with patch("app.langgraph_agents.create_langgraph_coordinator", return_value=coordinator):
            response = self.client.post(
                f"/api/cases/{self.case_id}/agent-chat",
                json={
                    "question": "它的付款期限呢？",
                    "conversation_id": conversation_id,
                    "mode": "langgraph",
                    "use_llm": False,
                    "use_remote_embeddings": False,
                },
            )
        self.assertEqual(response.status_code, 500, response.text)
        return response.json()["detail"]["run_id"]

    def test_resume_reuses_frozen_history_and_records_once(self):
        run_id = self.fail_run(self.conversation_id)
        # History changes after the failure: resume must keep the frozen snapshot.
        self.client.post(
            f"/api/cases/{self.case_id}/chat",
            json={"question": "乙公司采购合同的总价是多少？", "conversation_id": self.conversation_id,
                  "use_llm": False, "use_remote_embeddings": False},
        )
        response = self.client.post(f"/api/agent-runs/{run_id}/resume")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["conversation_id"], self.conversation_id)
        self.assertEqual(payload["context"]["restored"], True)
        self.assertEqual(payload["context"]["resolution"]["object_document_ids"], [self.ids["jia_contract"]])
        self.assertEqual(payload["resume_count"], 1)
        from app.db import connect

        with closing(connect()) as conn:
            saved = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE source_run_id=?", (run_id,)
            ).fetchone()[0]
        self.assertEqual(saved, 1)

    def test_resume_is_refused_after_conversation_is_archived(self):
        from app.db import now, transaction

        run_id = self.fail_run(self.conversation_id)
        with transaction() as conn:
            conn.execute(
                "UPDATE conversations SET archived_at=?, archive_expires_at=? WHERE id=?",
                (now(), 2**62, self.conversation_id),
            )
        response = self.client.post(f"/api/agent-runs/{run_id}/resume")
        self.assertEqual(response.status_code, 409, response.text)


if __name__ == "__main__":
    unittest.main()
