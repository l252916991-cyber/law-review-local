"""Usage panel: the payload separates unknown from zero and the renderer keeps that split."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.llm.llm_usage import record_llm_call
from app.main import app
from app.storage.db import db_scope, init_db, now, transaction

ROOT = Path(__file__).resolve().parents[1]


class UsagePanelPayloadTests(unittest.TestCase):
    """The API must hand the page the sample sizes behind every aggregate."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-usage-panel-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _metrics(self, case_id: int) -> dict:
        response = self.client.get(f"/api/cases/{case_id}/platform-metrics")
        self.assertEqual(response.status_code, 200)
        return response.json()["llm_usage"]

    def test_partial_usage_reports_the_known_subset_and_its_coverage(self):
        record_llm_call(case_id=1, kind="chat", model="local-a", usage={"prompt_tokens": 10, "completion_tokens": 4}, latency_ms=120, ttft_ms=40)
        record_llm_call(case_id=1, kind="chat", model="local-a", usage=None, latency_ms=300)
        record_llm_call(case_id=1, kind="critic", model="local-b", usage={"prompt_tokens": 20, "completion_tokens": 6}, latency_ms=200, ttft_ms=60, status="failed")

        usage = self._metrics(1)
        self.assertEqual(usage["calls"], 3)
        self.assertEqual(usage["failed"], 1)
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["input_tokens_known_calls"], 2)
        self.assertEqual(usage["output_tokens_known_calls"], 2)
        self.assertEqual(usage["ttft_known_calls"], 2)
        self.assertEqual(usage["ttft_p50_ms"], 50.0)
        self.assertEqual(usage["latency_p50_ms"], 200.0)
        # Groups must carry their own denominators: a single summed column would
        # let a browser read "30 tokens" as the complete total for three calls.
        chat = next(row for row in usage["by_kind"] if row["kind"] == "chat")
        self.assertEqual(chat["calls"], 2)
        self.assertEqual(chat["input_tokens_known_calls"], 1)
        self.assertEqual(chat["ttft_known_calls"], 1)
        models = {row["model"]: row for row in usage["by_model"]}
        self.assertEqual(set(models), {"local-a", "local-b"})
        self.assertEqual(models["local-b"]["failed"], 1)

    def test_a_case_without_calls_reports_no_measurements_rather_than_zero(self):
        case_id = self.client.post("/api/cases", json={"title": "空用量案件"}).json()["id"]
        usage = self._metrics(case_id)
        self.assertEqual(usage["calls"], 0)
        self.assertEqual(usage["failed"], 0)
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["ttft_p50_ms"])
        self.assertIsNone(usage["latency_p50_ms"])
        self.assertEqual(usage["input_tokens_known_calls"], 0)
        self.assertEqual(usage["ttft_known_calls"], 0)
        self.assertEqual(usage["by_kind"], [])
        self.assertEqual(usage["by_model"], [])

    def test_calls_belong_to_one_case_only(self):
        other_id = self.client.post("/api/cases", json={"title": "另一案件"}).json()["id"]
        with transaction() as conn:
            conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)",
                ("第三方案件", now(), now()),
            )
        record_llm_call(case_id=1, kind="chat", model="local-a", usage={"prompt_tokens": 5, "completion_tokens": 1}, latency_ms=80, ttft_ms=20)
        self.assertEqual(self._metrics(1)["calls"], 1)
        self.assertEqual(self._metrics(other_id)["calls"], 0)

    def test_missing_case_is_rejected(self):
        response = self.client.get("/api/cases/9999/platform-metrics")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
