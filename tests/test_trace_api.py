"""Run-trace API: the timeline needs per-step input/output, not just summaries."""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.storage.db import db_scope, init_db, now, transaction


class RunTraceApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-trace-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            case_id = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", ("轨迹案", now(), now())
            ).lastrowid
            self.run_id = conn.execute(
                """INSERT INTO agent_runs(case_id,question,route,status,total_ms,created_at,finished_at)
                   VALUES (?,?,?,'completed',1200,?,?)""",
                (case_id, "问题", "事实检索", now(), now()),
            ).lastrowid
            for index, node in enumerate(("planner", "retrieval", "critic"), start=1):
                conn.execute(
                    """INSERT INTO agent_steps(run_id,node_name,agent_role,status,input_json,output_json,
                                               latency_ms,started_at,finished_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        self.run_id,
                        node,
                        f"{node} 节点",
                        "completed",
                        json.dumps({"step": index}, ensure_ascii=False),
                        json.dumps({"summary": f"{node} 完成"}, ensure_ascii=False),
                        index * 100,
                        now(),
                        now(),
                    ),
                )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_trace_returns_steps_with_parsed_input_and_output(self):
        response = self.client.get(f"/api/agent-runs/{self.run_id}")
        self.assertEqual(response.status_code, 200)
        trace = response.json()
        self.assertEqual(trace["status"], "completed")
        self.assertEqual([step["node_name"] for step in trace["steps"]], ["planner", "retrieval", "critic"])
        self.assertEqual(trace["steps"][1]["input"], {"step": 2})
        self.assertEqual(trace["steps"][1]["output"]["summary"], "retrieval 完成")
        self.assertEqual(trace["steps"][0]["latency_ms"], 100)
        self.assertEqual(trace["citations"], [])

    def test_unknown_run_is_not_found(self):
        response = self.client.get("/api/agent-runs/999999")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
