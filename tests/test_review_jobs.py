"""Background review contracts without model requests or production state."""

import builtins
import json
import os
import tempfile
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import BoundedSemaphore, Event
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import review_jobs
from app.db import _scoped_db, connect, db_scope, init_db, now, transaction
from app.security import AccessMiddleware, Principal, _principal, current_principal


class DeferredExecutor:
    def __init__(self):
        self.items = []

    def submit(self, function, *args):
        future = Future()
        self.items.append((future, function, args))
        return future

    def run_next(self):
        future, function, args = self.items.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(function(*args))
            except BaseException as exc:
                future.set_exception(exc)
                raise


class FakeCoordinator:
    def __init__(self, case_id, runtime="native", fail=False):
        self.case_id, self.runtime, self.fail = case_id, runtime, fail
        self.principal = None
        self.start_value = None

    def _start_run(self, question, route):
        with transaction() as conn:
            run_id = conn.execute(
                "INSERT INTO agent_runs(case_id,question,route,status,runtime,created_at) VALUES (?,?,?,'running',?,?)",
                (self.case_id, question, route, self.runtime, now()),
            ).lastrowid
        return (run_id, f"agent-run-{run_id}") if self.runtime == "langgraph" else run_id

    def process_query(self, question, user_name, use_llm):
        self.principal = current_principal()
        self.start_value = self._start_run(question, "general")
        run_id = self.start_value[0] if isinstance(self.start_value, tuple) else self.start_value
        if self.fail:
            with transaction() as conn:
                conn.execute("UPDATE agent_runs SET status='failed' WHERE id=?", (run_id,))
            raise ValueError("sensitive implementation detail must not leak")
        with transaction() as conn:
            conn.execute("UPDATE agent_runs SET status='completed' WHERE id=?", (run_id,))
        return {"answer": "测试回答，需律师复核", "route": "general", "citations": [],
                "run_id": run_id, "runtime": self.runtime, "llm_used": False}


class ReviewJobTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-review-jobs-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "law_review.db"
        scope = db_scope(self.path)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        self.executor = DeferredExecutor()
        executor_patch = patch.object(review_jobs, "_executor", self.executor)
        executor_patch.start()
        self.addCleanup(executor_patch.stop)
        admission_patch = patch.object(review_jobs, "_admission", BoundedSemaphore(4))
        admission_patch.start()
        self.addCleanup(admission_patch.stop)
        env_patch = patch.dict(os.environ, {"LAW_REVIEW_AUTH_MODE": "local"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        application = FastAPI()
        application.add_middleware(AccessMiddleware)
        application.include_router(review_jobs.router)
        self.client = TestClient(application)
        self.addCleanup(self.client.close)

    def submit(self, **kwargs):
        body = {"question": "募集资金流向？", "use_llm": False, "use_remote_embeddings": False, **kwargs}
        response = self.client.post("/api/cases/1/agent-jobs", json=body)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["job_id"]

    def status(self, job_id):
        response = self.client.get(f"/api/agent-jobs/{job_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_all_slots_available(self):
        acquired = [review_jobs._admission.acquire(blocking=False) for _ in range(4)]
        self.assertEqual(acquired, [True] * 4)
        self.assertFalse(review_jobs._admission.acquire(blocking=False))
        for _ in acquired:
            review_jobs._admission.release()

    def test_native_start_id_and_persisted_conversation(self):
        coordinator = FakeCoordinator(1)
        job_id = self.submit()
        with patch("app.agents.create_coordinator", return_value=coordinator) as factory:
            self.executor.run_next()
        factory.assert_called_once_with(1, False)
        payload = self.status(job_id)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["run_id"], coordinator.start_value)
        self.assertEqual(payload["result"]["mode"], "multi_agent")
        with closing(connect()) as conn:
            messages = conn.execute("SELECT role FROM messages WHERE conversation_id=? ORDER BY id",
                                    (payload["result"]["conversation_id"],)).fetchall()
        self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
        self.assert_all_slots_available()

    def test_langgraph_start_tuple_return_preserved(self):
        coordinator = FakeCoordinator(1, "langgraph")
        job_id = self.submit(mode="langgraph")
        with patch("app.langgraph_agents.create_langgraph_coordinator", return_value=coordinator) as factory:
            self.executor.run_next()
        factory.assert_called_once_with(1, False)
        self.assertIsInstance(coordinator.start_value, tuple)
        self.assertEqual(self.status(job_id)["run_id"], coordinator.start_value[0])
        self.assert_all_slots_available()

    def test_native_does_not_import_langgraph(self):
        coordinator = FakeCoordinator(1)
        job_id = self.submit()
        original_import = builtins.__import__

        def no_langgraph(name, *args, **kwargs):
            if name == "langgraph_agents":
                raise ImportError("LangGraph unavailable")
            return original_import(name, *args, **kwargs)

        with patch("app.agents.create_coordinator", return_value=coordinator), patch("builtins.__import__", no_langgraph):
            self.executor.run_next()
        self.assertEqual(self.status(job_id)["status"], "completed")

    def test_missing_langgraph_dependency_persists_failure_releases_slot(self):
        job_id = self.submit(mode="langgraph")
        original_import = builtins.__import__

        def no_langgraph(name, *args, **kwargs):
            if name == "langgraph_agents":
                raise ImportError("LangGraph unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", no_langgraph):
            self.executor.run_next()
        self.assertEqual(self.status(job_id)["error"]["type"], "ImportError")
        self.assert_all_slots_available()

    def test_native_failure_retains_tracked_run_and_sanitizes_error(self):
        coordinator = FakeCoordinator(1, fail=True)
        job_id = self.submit()
        with patch("app.agents.create_coordinator", return_value=coordinator):
            self.executor.run_next()
        payload = self.status(job_id)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"]["run_id"], coordinator.start_value)
        self.assertNotIn("sensitive", json.dumps(payload))
        self.assert_all_slots_available()

    def test_queue_full_returns_429_without_extra_database_row(self):
        for _ in range(4):
            self.submit()
        response = self.client.post("/api/cases/1/agent-jobs", json={"question": "第五个请求"})
        self.assertEqual(response.status_code, 429)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0], 4)
        for future, _, _ in self.executor.items:
            future.cancel()
        self.assert_all_slots_available()

    def test_executor_submit_failure_returns_503_and_persists_diagnostic(self):
        with patch.object(self.executor, "submit", side_effect=RuntimeError("executor stopped")):
            response = self.client.post("/api/cases/1/agent-jobs", json={"question": "请求失败"})
        self.assertEqual(response.status_code, 503)
        job_id = response.json()["detail"]["job_id"]
        self.assertEqual(self.status(job_id)["status"], "failed")
        self.assertEqual(self.status(job_id)["error"]["type"], "RuntimeError")
        self.assert_all_slots_available()

    def test_canceled_queued_future_marks_interrupted_and_never_runs(self):
        job_id = self.submit()
        self.executor.items[0][0].cancel()
        with patch("app.agents.create_coordinator", side_effect=AssertionError("must not run")):
            self.executor.run_next()
        self.assertEqual(self.status(job_id)["status"], "interrupted")
        self.assert_all_slots_available()

    def test_restart_interrupts_queued_jobs_and_skips_late_executor(self):
        job_id = self.submit(mode="langgraph")
        init_db(seed=False, recover_runs=True)
        with patch("app.langgraph_agents.create_langgraph_coordinator", side_effect=AssertionError("must not run")):
            self.executor.run_next()
        payload = self.status(job_id)
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["error"]["type"], "Interrupted")
        self.assertFalse(payload["resumable"])
        self.assert_all_slots_available()

    def test_interrupted_langgraph_reports_trace_resumability(self):
        job_id = self.submit(mode="langgraph")
        run_id, _ = FakeCoordinator(1, "langgraph")._start_run("test", "general")
        with transaction() as conn:
            conn.execute("UPDATE review_jobs SET run_id=? WHERE id=?", (run_id, job_id))
        init_db(seed=False, recover_runs=True)
        with patch("app.agents.get_run_trace", return_value={"runtime": "langgraph", "resumable": True, "steps": []}):
            self.assertTrue(self.status(job_id)["resumable"])
        self.executor.items[0][0].cancel()

    def test_authenticated_principal_and_database_scope_survive_deferred_execution(self):
        coordinator = FakeCoordinator(1)
        name = "真实律师"
        token = "t" * 40
        env = {"LAW_REVIEW_AUTH_MODE": "token", "LAW_REVIEW_API_TOKENS_JSON": json.dumps({token: {"name": name, "admin": False, "case_ids": [1]}})}
        with patch.dict(os.environ, env):
            response = self.client.post("/api/cases/1/agent-jobs", json={"question": "请求", "user_name": "伪造作者", "use_llm": False},
                                        headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
        with tempfile.TemporaryDirectory(prefix="other-db-") as alternate:
            with db_scope(Path(alternate) / "unused.db"), patch("app.agents.create_coordinator", return_value=coordinator):
                self.executor.run_next()
                self.assertFalse((Path(alternate) / "unused.db").exists())
        payload = self.status(job_id)
        self.assertEqual(payload["result"]["user_name"], name)
        self.assertEqual(coordinator.principal.name, name)
        self.assertFalse(coordinator.principal.admin)

    def test_environment_derived_database_is_frozen_before_execution(self):
        coordinator = FakeCoordinator(1)
        marker = _scoped_db.set(None)
        try:
            with patch.dict(os.environ, {"LAW_REVIEW_DATA_DIR": str(self.path.parent)}):
                job_id = self.submit()
            with tempfile.TemporaryDirectory(prefix="wrong-env-db-") as alternate:
                with patch.dict(os.environ, {"LAW_REVIEW_DATA_DIR": alternate}), patch("app.agents.create_coordinator", return_value=coordinator):
                    self.executor.run_next()
                    self.assertFalse((Path(alternate) / "law_review.db").exists())
        finally:
            _scoped_db.reset(marker)
        self.assertEqual(self.status(job_id)["status"], "completed")

    def test_cross_case_access_and_missing_job_denied(self):
        token = _principal.set(Principal("受限律师", False, (99,)))
        try:
            with self.assertRaises(Exception) as denied:
                review_jobs.submit_review(1, review_jobs.ReviewJobRequest(question="越权"))
            self.assertEqual(denied.exception.status_code, 404)
        finally:
            _principal.reset(token)
        self.assertEqual(self.client.get("/api/agent-jobs/missing").status_code, 404)
        self.assert_all_slots_available()

    def test_real_native_and_langgraph_offline_background_jobs(self):
        for mode in ("multi_agent", "langgraph"):
            with self.subTest(mode=mode):
                job_id = self.submit(mode=mode)
                self.executor.run_next()
                payload = self.status(job_id)
                self.assertEqual(payload["status"], "completed", payload)
                self.assertFalse(payload["result"]["llm_used"])
                self.assertTrue(payload["result"]["answer"])
        self.assert_all_slots_available()

    def test_shutdown_cancels_queued_and_drains_running_executor(self):
        block = Event()
        executor = ThreadPoolExecutor(max_workers=1)
        executor.submit(block.wait, 2)
        with patch.object(review_jobs, "_executor", executor):
            job_id = self.submit()
            block.set()
            review_jobs.shutdown_review_executor()
            self.assertIsNone(review_jobs._executor)
            # Depending on scheduling the accepted job is either drained or
            # canceled, never left indefinitely queued/running.
            self.assertIn(self.status(job_id)["status"], {"completed", "interrupted"})
            review_jobs.start_review_executor()
            self.assertIsNotNone(review_jobs._executor)
            review_jobs.shutdown_review_executor()


if __name__ == "__main__":
    unittest.main()
