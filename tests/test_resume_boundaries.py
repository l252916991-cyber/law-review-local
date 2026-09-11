"""Case access, checkpoint identity, and concurrent-resume regression tests."""

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agents import CriticAgent
from app.db import connect, db_scope, init_db, now, transaction
from app.langgraph_agents import LangGraphRunError, create_langgraph_coordinator
from app.main import app
from app.security import Principal, _principal


class ResumeBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-resume-boundary-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "law_review.db"
        self.checkpoint = Path(directory.name) / "checkpoints.sqlite"
        scope = db_scope(self.path)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        with transaction() as conn:
            self.other_case = conn.execute("INSERT INTO cases(title,description,created_at,updated_at) VALUES (?,?,?,?)",
                                           ("隔离私有案件", "其他案件秘密", now(), now())).lastrowid
        self.token = "r" * 40
        environment = patch.dict(os.environ, {
            "LAW_REVIEW_AUTH_MODE": "token", "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
            "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(self.checkpoint),
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({self.token: {"name": "案件一律师", "case_ids": [1]}}),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def coordinator(self, case_id=1, **kwargs):
        return create_langgraph_coordinator(case_id, False, checkpoint_path=self.checkpoint, **kwargs)

    @contextmanager
    def direct_principal(self, case_id=1):
        marker = _principal.set(
            Principal("LangGraph fixture lawyer", False, (case_id,), permissions=frozenset({"view"}))
        )
        try:
            yield
        finally:
            _principal.reset(marker)

    def fail_memory(self, case_id=1):
        def failure(node, state):
            if node == "memory":
                raise RuntimeError("test interruption before memory commit")
        with self.direct_principal(case_id), self.assertRaises(LangGraphRunError) as caught:
            self.coordinator(case_id, failure_injector=failure).process_query("检查本案证据疏漏", use_llm=False)
        with closing(connect()) as conn:
            failed_step = conn.execute(
                "SELECT status FROM agent_steps WHERE run_id=? AND node_name='memory'", (caught.exception.run_id,)
            ).fetchone()
        self.assertIsNotNone(failed_step)
        self.assertEqual(failed_step["status"], "failed")
        return caught.exception.run_id

    def test_token_mode_direct_call_without_principal_is_denied(self):
        with self.assertRaises(LangGraphRunError) as caught:
            self.coordinator().process_query("检查本案证据疏漏", use_llm=False)
        self.assertEqual(getattr(caught.exception.__cause__, "status_code", None), 401)
        with closing(connect()) as conn:
            failed_step = conn.execute(
                "SELECT status,output_json FROM agent_steps WHERE run_id=? AND node_name='retrieve'",
                (caught.exception.run_id,),
            ).fetchone()
        self.assertIsNotNone(failed_step)
        self.assertEqual(failed_step["status"], "failed")
        self.assertEqual(json.loads(failed_step["output_json"])["diagnostic"]["error_type"], "HTTPException")

    def test_scoped_export_allowed_only_for_own_case(self):
        response = self.client.get("/api/cases/1/export", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text[:300] if response.status_code != 200 else "")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn("案件摘要.md", archive.namelist())
            self.assertNotIn("其他案件秘密", archive.read("案件摘要.md").decode())
        with patch("app.main.build_export", side_effect=AssertionError("must not export another case")):
            denied = self.client.get(f"/api/cases/{self.other_case}/export", headers=self.headers)
        self.assertEqual(denied.status_code, 404)

    def test_cross_case_resume_is_denied_before_invocation(self):
        run_id = self.fail_memory(self.other_case)
        with patch("app.langgraph_agents.resume_langgraph_run", side_effect=AssertionError("must not resume")):
            response = self.client.post(f"/api/agent-runs/{run_id}/resume", headers=self.headers)
        self.assertEqual(response.status_code, 404)
        with closing(connect()) as conn:
            run = conn.execute("SELECT status,resume_count FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual((run["status"], run["resume_count"]), ("failed", 0))

    def test_wrong_coordinator_case_rejected_even_without_http(self):
        run_id = self.fail_memory()
        with self.direct_principal(self.other_case), self.assertRaises(KeyError):
            self.coordinator(self.other_case).resume(run_id)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT resume_count FROM agent_runs WHERE id=?", (run_id,)).fetchone()[0], 0)

    def test_checkpoint_state_case_mismatch_rejected_before_claim(self):
        run_id = self.fail_memory()
        coordinator = self.coordinator()
        connection = coordinator._checkpoint_connection()
        try:
            graph = coordinator._build_graph(SqliteSaver(connection))
            graph.update_state({"configurable": {"thread_id": f"agent-run-{run_id}"}}, {"case_id": self.other_case})
        finally:
            connection.close()
        with self.direct_principal(), self.assertRaisesRegex(ValueError, "Checkpoint"):
            coordinator.resume(run_id)
        with closing(connect()) as conn:
            row = conn.execute("SELECT status,resume_count FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(tuple(row), ("failed", 0))

    def test_coordinator_keeps_original_database_for_run_and_resume(self):
        coordinator = self.coordinator()
        run_id = self.fail_memory()
        with tempfile.TemporaryDirectory(prefix="lexvault-unrelated-") as other:
            alternate = Path(other) / "must-not-exist.db"
            with db_scope(alternate), self.direct_principal():
                result = coordinator.process_query("资金流向", use_llm=False)
                recovered = coordinator.resume(run_id)
            self.assertFalse(alternate.exists())
        with closing(connect()) as conn:
            for item in (result, recovered):
                self.assertEqual(conn.execute("SELECT status FROM agent_runs WHERE id=?", (item["run_id"],)).fetchone()[0], "completed")

    def test_concurrent_resume_executes_memory_once_and_never_repeats_critic(self):
        critic_calls = []
        original_critic = CriticAgent.run

        def count_critic(instance, *args, **kwargs):
            critic_calls.append(1)
            return original_critic(instance, *args, **kwargs)

        with patch.object(CriticAgent, "run", count_critic):
            run_id = self.fail_memory()
            barrier = threading.Barrier(2)
            release = threading.Event()
            resumed_nodes = []

            def pause_memory(node, state):
                resumed_nodes.append(node)
                if node == "memory" and not release.wait(5):
                    raise TimeoutError("test resume release timeout")

            coordinators = [self.coordinator(failure_injector=pause_memory) for _ in range(2)]
            for coordinator in coordinators:
                original_available = coordinator.checkpoint_available

                def synchronized_available(thread_id, original=original_available):
                    available = original(thread_id)
                    barrier.wait(timeout=3)
                    return available

                coordinator.checkpoint_available = synchronized_available

            with ThreadPoolExecutor(max_workers=2) as executor:
                def resume_as_fixture(coordinator):
                    with self.direct_principal():
                        return coordinator.resume(run_id)

                futures = [executor.submit(resume_as_fixture, coordinator) for coordinator in coordinators]
                try:
                    first = next(as_completed(futures, timeout=4))
                    with self.assertRaises(ValueError):
                        first.result()
                finally:
                    release.set()
                results = []
                for future in futures:
                    try:
                        results.append(future.result(timeout=4))
                    except ValueError:
                        pass
        self.assertEqual(len(results), 1)
        self.assertEqual(len(critic_calls), 1)
        self.assertEqual(resumed_nodes, ["memory"])
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT resume_count FROM agent_runs WHERE id=?", (run_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories WHERE source_run_id=?", (run_id,)).fetchone()[0], 1)
            duplicates = conn.execute("SELECT node_name,COUNT(*) FROM agent_steps WHERE run_id=? GROUP BY node_name HAVING COUNT(*)>1", (run_id,)).fetchall()
        self.assertEqual(duplicates, [])


if __name__ == "__main__":
    unittest.main()
