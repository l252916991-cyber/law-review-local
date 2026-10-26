"""Human review tasks: assignment, transitions, returns and due dates."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import db_scope, init_db, now, transaction
from app.main import app

ADMIN_TOKEN = "b" * 40


class TaskWorkflowTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-tasks-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        scope = db_scope(self.root / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case_a = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('任务甲案',?,?)", (now(), now())
            ).lastrowid
            self.case_b = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('任务乙案',?,?)", (now(), now())
            ).lastrowid
        self.env = patch.dict(
            "os.environ",
            {
                "LAW_REVIEW_AUTH_MODE": "token",
                "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
                "LAW_REVIEW_API_TOKENS_JSON": json.dumps({
                    ADMIN_TOKEN: {"name": "管理员", "admin": True},
                    "c" * 40: {"name": "部署律师", "case_ids": [self.case_b],
                               "permissions": ["view", "edit", "approve", "export"]},
                }),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        self.owner, self.owner_token = self.member("承办律师", "owner", assignments=[self.case_a])
        self.assistant, self.assistant_token = self.member("助理", "assistant", assignments=[self.case_a])
        self.reviewer, self.reviewer_token = self.member(
            "复核律师", "reviewer", assignments=[self.case_a], permissions=["view", "approve", "export"]
        )

    def member(self, name, role, assignments, permissions=None):
        response = self.client.post(
            "/api/members", headers=self.admin,
            json={
                "name": name,
                "permissions": permissions or ["view", "edit", "approve", "export"],
                "assignments": [{"case_id": case_id, "role": role} for case_id in assignments],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["member"], response.json()["token"]

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def create_task(self, **overrides):
        payload = {
            "title": "核对借款合同原件",
            "kind": "核证",
            "detail": "确认合同签署日期与金额",
            "assignee_member_id": self.assistant["id"],
            "due_at": (date.today() + timedelta(days=3)).isoformat(),
        }
        payload.update(overrides)
        response = self.client.post(f"/api/cases/{self.case_a}/tasks", headers=self.headers(self.owner_token), json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["task"]

    def act(self, task_id, token, action, **extra):
        return self.client.patch(
            f"/api/tasks/{task_id}", headers=self.headers(token), json={"action": action, **extra}
        )

    def test_full_flow_with_a_returned_task(self):
        task = self.create_task()
        self.assertEqual(task["status"], "待处理")
        self.assertEqual(task["assignee_name"], "助理")
        self.assertFalse(task["overdue"])

        started = self.act(task["id"], self.assistant_token, "start")
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["task"]["status"], "进行中")

        submitted = self.act(task["id"], self.assistant_token, "submit", note="已核对合同第2页")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["task"]["status"], "待复核")

        returned = self.act(task["id"], self.reviewer_token, "return", note="缺少签署页，请补拍")
        self.assertEqual(returned.status_code, 200, returned.text)
        body = returned.json()["task"]
        self.assertEqual(body["status"], "待处理")
        self.assertEqual(body["events"][-1]["note"], "缺少签署页，请补拍")
        self.assertEqual(body["events"][-1]["actor"], "复核律师")
        self.assertEqual([event["action"] for event in body["events"]],
                         ["create", "start", "submit", "return"])

        self.act(task["id"], self.assistant_token, "start")
        self.act(task["id"], self.assistant_token, "submit")
        completed = self.act(task["id"], self.reviewer_token, "complete", note="复核通过")
        self.assertEqual(completed.status_code, 200, completed.text)
        body = completed.json()["task"]
        self.assertEqual(body["status"], "已完成")
        self.assertTrue(body["closed_at"])
        self.assertEqual(body["events"][-1]["note"], "复核通过")

        # A finished task is closed for further changes.
        self.assertEqual(self.act(task["id"], self.assistant_token, "start").status_code, 409)

    def test_return_requires_a_reason(self):
        task = self.create_task()
        self.act(task["id"], self.assistant_token, "start")
        self.act(task["id"], self.assistant_token, "submit")
        rejected = self.act(task["id"], self.reviewer_token, "return", note="   ")
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("原因", rejected.json()["detail"])

    def test_role_and_assignee_checks(self):
        task = self.create_task()
        # The reviewer may not create tasks (needs edit + owner role).
        denied_create = self.client.post(
            f"/api/cases/{self.case_a}/tasks", headers=self.headers(self.reviewer_token),
            json={"title": "越权建任务", "kind": "复核"},
        )
        self.assertEqual(denied_create.status_code, 403)
        # A different member cannot submit someone else's task.
        other, other_token = self.member("另一位助理", "assistant", assignments=[self.case_a])
        self.act(task["id"], self.assistant_token, "start")
        denied_submit = self.act(task["id"], other_token, "submit")
        self.assertEqual(denied_submit.status_code, 403)
        # The assistant cannot confirm completion.
        self.act(task["id"], self.assistant_token, "submit")
        denied_complete = self.act(task["id"], self.assistant_token, "complete")
        self.assertEqual(denied_complete.status_code, 403)
        # Reassignment stays with the case manager.
        denied_assign = self.act(task["id"], self.assistant_token, "assign", assignee_member_id=other["id"])
        self.assertEqual(denied_assign.status_code, 403)
        reassigned = self.act(task["id"], self.owner_token, "assign", assignee_member_id=other["id"])
        self.assertEqual(reassigned.status_code, 200, reassigned.text)
        self.assertEqual(reassigned.json()["task"]["assignee_name"], "另一位助理")

    def test_mine_lists_only_open_assigned_tasks(self):
        first = self.create_task(title="我的任务甲")
        second = self.create_task(title="我的任务乙")
        self.act(second["id"], self.assistant_token, "start")
        self.act(second["id"], self.assistant_token, "submit")
        self.act(second["id"], self.reviewer_token, "complete")

        mine = self.client.get("/api/tasks/mine", headers=self.headers(self.assistant_token))
        self.assertEqual(mine.status_code, 200, mine.text)
        titles = [row["title"] for row in mine.json()["tasks"]]
        self.assertEqual(titles, ["我的任务甲"])
        self.assertEqual(mine.json()["counts"]["待处理"], 1)

        # reviewers see nothing unless a task was assigned to them
        reviewer_mine = self.client.get("/api/tasks/mine", headers=self.headers(self.reviewer_token))
        self.assertEqual(reviewer_mine.json()["tasks"], [])

    def test_due_date_validation_and_overdue_flag(self):
        task = self.create_task(due_at=(date.today() - timedelta(days=1)).isoformat())
        self.assertTrue(task["overdue"])
        bad = self.client.post(
            f"/api/cases/{self.case_a}/tasks", headers=self.headers(self.owner_token),
            json={"title": "错误期限", "kind": "阅卷", "due_at": "2026/10/27"},
        )
        self.assertEqual(bad.status_code, 400)
        refreshed = self.act(task["id"], self.owner_token, "due", due_at=None)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertIsNone(refreshed.json()["task"]["due_at"])
        self.assertFalse(refreshed.json()["task"]["overdue"])

    def test_case_isolation_for_tasks(self):
        task = self.create_task()
        # 部署律师 only has case B: a task in case A is not addressable.
        hidden = self.client.get(
            f"/api/cases/{self.case_a}/tasks", headers=self.headers("c" * 40)
        )
        self.assertEqual(hidden.status_code, 404)
        hidden_action = self.act(task["id"], "c" * 40, "start")
        self.assertEqual(hidden_action.status_code, 404)
        listed = self.client.get(f"/api/tasks/mine", headers=self.headers("c" * 40))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["tasks"], [])

    def test_task_transitions_are_audited(self):
        from contextlib import closing
        from app.db import connect

        task = self.create_task()
        self.act(task["id"], self.assistant_token, "start")
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT detail FROM audit_log WHERE case_id = ? AND action = '任务流转' ORDER BY id",
                (self.case_a,),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn(f"任务#{task['id']}", rows[0]["detail"])
        self.assertIn("start", rows[0]["detail"])
        self.assertIn("助理", rows[0]["detail"])


if __name__ == "__main__":
    unittest.main()
