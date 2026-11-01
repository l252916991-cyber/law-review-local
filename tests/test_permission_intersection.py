"""A case role never restores a permission that was withdrawn.

Review and management actions require the member permission *and* the matching
case role; the middleware's coarse check is not the authorization. These
regressions cover the R1 matrix in
``docs/history/migration/collaboration-release-readiness-plan-20261027.md``.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import db_scope, init_db, now, transaction
from app.main import app

ADMIN_TOKEN = "b" * 40
DEPLOY_TOKEN = "d" * 40


class PermissionIntersectionCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-permission-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        scope = db_scope(self.root / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case_a = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('权限甲案',?,?)", (now(), now())
            ).lastrowid
            self.case_b = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('权限乙案',?,?)", (now(), now())
            ).lastrowid
        self.env = patch.dict(
            "os.environ",
            {
                "LAW_REVIEW_AUTH_MODE": "token",
                "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
                "LAW_REVIEW_API_TOKENS_JSON": json.dumps({
                    ADMIN_TOKEN: {"name": "管理员", "admin": True},
                    # A deployment token has case scope but no case role.
                    DEPLOY_TOKEN: {"name": "部署律师", "case_ids": [self.case_a],
                                   "permissions": ["view", "edit", "approve", "export"]},
                }),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.admin = self.headers(ADMIN_TOKEN)

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def member(self, name, role, permissions, case_id=None):
        response = self.client.post(
            "/api/members", headers=self.admin,
            json={"name": name, "permissions": permissions,
                  "assignments": [{"case_id": case_id or self.case_a, "role": role}]},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return body["member"], body["token"]

    def create_report(self, case_id=None):
        case_id = case_id or self.case_a
        created = self.client.post(f"/api/cases/{case_id}/reports", headers=self.admin, json={"title": "结案报告"})
        self.assertEqual(created.status_code, 201, created.text)
        report_id = created.json()["report"]["id"]
        submitted = self.client.patch(
            f"/api/reports/{report_id}", headers=self.admin, json={"action": "submit"}
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        return report_id

    def create_task(self, assignee_id=None, case_id=None):
        case_id = case_id or self.case_a
        response = self.client.post(
            f"/api/cases/{case_id}/tasks", headers=self.admin,
            json={"title": "核对证据", "kind": "核证", "assignee_member_id": assignee_id},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["task"]


class ReportPermissionTest(PermissionIntersectionCase):
    def test_role_with_view_only_cannot_approve_or_return(self):
        member, token = self.member("只读复核", "reviewer", ["view"])
        report_id = self.create_report()

        denied = self.client.patch(
            f"/api/reports/{report_id}", headers=self.headers(token), json={"action": "approve"}
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertIn("审批确认", denied.json()["detail"])
        denied_return = self.client.patch(
            f"/api/reports/{report_id}", headers=self.headers(token), json={"action": "return", "note": "不通过"}
        )
        self.assertEqual(denied_return.status_code, 403, denied_return.text)
        denied_void = self.client.patch(
            f"/api/reports/{report_id}", headers=self.headers(token), json={"action": "void"}
        )
        self.assertEqual(denied_void.status_code, 403, denied_void.text)

        listed = self.client.get(f"/api/cases/{self.case_a}/reports", headers=self.headers(token)).json()
        self.assertFalse(listed["reports"][0]["can_review"])
        self.assertEqual(listed["reports"][0]["status"], "待复核")
        self.assertIsNone(listed["reports"][0]["approved_by"])

    def test_owner_role_with_view_only_cannot_approve_or_void(self):
        member, token = self.member("只读承办", "owner", ["view"])
        report_id = self.create_report()
        denied = self.client.patch(
            f"/api/reports/{report_id}", headers=self.headers(token), json={"action": "approve"}
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(
            self.client.patch(
                f"/api/reports/{report_id}", headers=self.headers(token), json={"action": "void"}
            ).status_code,
            403,
        )
        listed = self.client.get(f"/api/cases/{self.case_a}/reports", headers=self.headers(token)).json()
        self.assertFalse(listed["reports"][0]["can_review"])
        self.assertEqual(listed["reports"][0]["status"], "待复核")

    def test_reviewer_without_edit_can_approve_but_not_author(self):
        member, token = self.member("复核律师", "reviewer", ["view", "approve", "export"])
        created = self.client.post(
            f"/api/cases/{self.case_a}/reports", headers=self.headers(token), json={}
        )
        self.assertEqual(created.status_code, 403, created.text)

        report_id = self.create_report()
        approved = self.client.patch(
            f"/api/reports/{report_id}", headers=self.headers(token), json={"action": "approve"}
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["report"]["approved_by"], "复核律师")
        self.assertTrue(approved.json()["report"]["can_review"])
        self.assertFalse(approved.json()["report"]["can_edit"])

    def test_withdrawn_approve_applies_to_an_existing_session(self):
        member, token = self.member("复核律师", "reviewer", ["view", "approve"])
        session = TestClient(app)
        self.addCleanup(session.close)
        login = session.post("/api/auth/session", json={"token": token})
        self.assertEqual(login.status_code, 200, login.text)

        first = self.create_report()
        self.assertEqual(session.patch(f"/api/reports/{first}", json={"action": "approve"}).status_code, 200)

        updated = self.client.patch(
            f"/api/members/{member['id']}", headers=self.admin, json={"permissions": ["view"]}
        )
        self.assertEqual(updated.status_code, 200, updated.text)

        second = self.create_report()
        denied = session.patch(f"/api/reports/{second}", json={"action": "approve"})
        self.assertEqual(denied.status_code, 403, denied.text)
        # The same live session still reads; only the review action is gone.
        self.assertEqual(session.get(f"/api/cases/{self.case_a}/reports").status_code, 200)

    def test_environment_principal_without_case_role_cannot_review(self):
        headers = self.headers(DEPLOY_TOKEN)
        created = self.client.post(f"/api/cases/{self.case_a}/reports", headers=headers, json={})
        self.assertEqual(created.status_code, 201, created.text)
        report_id = created.json()["report"]["id"]
        self.client.patch(f"/api/reports/{report_id}", headers=headers, json={"action": "submit"})
        denied = self.client.patch(f"/api/reports/{report_id}", headers=headers, json={"action": "approve"})
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertFalse(
            self.client.get(f"/api/cases/{self.case_a}/reports", headers=headers).json()["reports"][0]["can_review"]
        )


class TaskPermissionTest(PermissionIntersectionCase):
    def test_owner_with_view_only_cannot_create_or_assign(self):
        member, token = self.member("只读承办", "owner", ["view"])
        task = self.create_task()
        headers = self.headers(token)
        self.assertEqual(
            self.client.post(f"/api/cases/{self.case_a}/tasks", headers=headers,
                             json={"title": "越权任务", "kind": "阅卷"}).status_code,
            403,
        )
        assign = self.client.patch(
            f"/api/tasks/{task['id']}", headers=headers,
            json={"action": "assign", "assignee_member_id": member["id"]},
        )
        self.assertEqual(assign.status_code, 403, assign.text)
        cancel = self.client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"action": "cancel"})
        self.assertEqual(cancel.status_code, 403, cancel.text)
        payload = self.client.get(f"/api/cases/{self.case_a}/tasks", headers=headers).json()
        self.assertFalse(payload["tasks"][0]["can_manage"])

    def test_assignee_needs_edit_permission_to_start_and_submit(self):
        member, token = self.member("助理", "assistant", ["view", "edit"])
        task = self.create_task(assignee_id=member["id"])
        headers = self.headers(token)
        self.assertEqual(
            self.client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"action": "start"}).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"action": "submit"}).status_code,
            200,
        )
        second = self.create_task(assignee_id=member["id"])
        revoked = self.client.patch(
            f"/api/members/{member['id']}", headers=self.admin, json={"permissions": ["view"]}
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        denied = self.client.patch(f"/api/tasks/{second['id']}", headers=headers, json={"action": "start"})
        self.assertEqual(denied.status_code, 403, denied.text)
        payload = self.client.get(f"/api/cases/{self.case_a}/tasks", headers=headers).json()
        flags = {row["id"]: row for row in payload["tasks"]}
        self.assertFalse(flags[second["id"]]["can_work"])

    def test_only_approver_can_return_or_complete(self):
        member, token = self.member("复核律师", "reviewer", ["view", "approve"])
        task = self.create_task()
        invalid_assignment = self.client.patch(
            f"/api/tasks/{task['id']}", headers=self.admin,
            json={"action": "assign", "assignee_member_id": member["id"]},
        )
        self.assertEqual(invalid_assignment.status_code, 409, invalid_assignment.text)
        submitted = self.client.patch(
            f"/api/tasks/{task['id']}", headers=self.headers(token), json={"action": "start"}
        )
        self.assertEqual(submitted.status_code, 403, submitted.text)

        owner, owner_token = self.member("承办律师", "owner", ["view", "edit"])
        assigned = self.client.patch(
            f"/api/tasks/{task['id']}", headers=self.admin, json={"action": "assign",
                                                                  "assignee_member_id": owner["id"]},
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(
            self.client.patch(
                f"/api/tasks/{task['id']}", headers=self.headers(owner_token), json={"action": "start"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/tasks/{task['id']}", headers=self.headers(owner_token), json={"action": "submit"}
            ).status_code,
            200,
        )
        denied = self.client.patch(
            f"/api/tasks/{task['id']}", headers=self.headers(owner_token), json={"action": "complete"}
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        completed = self.client.patch(
            f"/api/tasks/{task['id']}", headers=self.headers(token), json={"action": "complete"}
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["task"]["status"], "已完成")

    def test_task_flags_distinguish_manage_review_and_work(self):
        reviewer, reviewer_token = self.member("复核律师", "reviewer", ["view", "approve"])
        task = self.create_task()
        flags = self.client.get(f"/api/cases/{self.case_a}/tasks", headers=self.headers(reviewer_token)).json()
        row = flags["tasks"][0]
        self.assertFalse(row["can_manage"])
        self.assertTrue(row["can_review"])
        self.assertFalse(row["can_work"])
        self.assertEqual(row["id"], task["id"])


if __name__ == "__main__":
    unittest.main()
