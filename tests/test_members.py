"""Directory members, one-time tokens, per-case roles and revocation."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.storage.db import connect, db_scope, init_db, now, transaction

ADMIN_TOKEN = "b" * 40


class MemberTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-members-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        scope = db_scope(self.root / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case_a = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('甲案',?,?)", (now(), now())
            ).lastrowid
            self.case_b = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('乙案',?,?)", (now(), now())
            ).lastrowid
        self.env = patch.dict(
            "os.environ",
            {
                "LAW_REVIEW_AUTH_MODE": "token",
                "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
                "LAW_REVIEW_API_TOKENS_JSON": json.dumps({ADMIN_TOKEN: {"name": "管理员", "admin": True}}),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def create_member(self, name="律师甲", permissions=None, admin=False, assignments=None):
        payload = {
            "name": name,
            "admin": admin,
            "permissions": permissions or ["view", "edit", "approve", "export"],
            "assignments": assignments or [],
        }
        response = self.client.post("/api/members", headers=self.admin, json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return body["member"], body["token"]

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"}


class MemberTokenTest(MemberTestCase):
    def test_member_sees_only_assigned_cases(self):
        member, token = self.create_member(assignments=[{"case_id": self.case_a, "role": "owner"}])
        self.assertTrue(member["active"])
        self.assertEqual(member["cases"][0]["role"], "owner")

        listed = self.client.get("/api/cases", headers=self.headers(token))
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([row["id"] for row in listed.json()], [self.case_a])

        denied = self.client.get(f"/api/cases/{self.case_b}", headers=self.headers(token))
        self.assertEqual(denied.status_code, 404)
        # A directory member is not an administrator.
        self.assertEqual(self.client.get("/api/members", headers=self.headers(token)).status_code, 403)

    def test_login_issues_a_session_for_a_member_token(self):
        _, token = self.create_member(assignments=[{"case_id": self.case_a, "role": "viewer"}])
        login = self.client.post("/api/auth/session", json={"token": token})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()["name"], "律师甲")
        self.assertEqual(self.client.get("/api/cases").status_code, 200)

    def test_deactivation_invalidates_token_and_existing_session(self):
        member, token = self.create_member(assignments=[{"case_id": self.case_a, "role": "viewer"}])
        self.client.post("/api/auth/session", json={"token": token})
        self.assertEqual(self.client.get("/api/cases").status_code, 200)

        deactivated = self.client.patch(
            f"/api/members/{member['id']}", headers=self.admin, json={"active": False}
        )
        self.assertEqual(deactivated.status_code, 200, deactivated.text)
        self.assertFalse(deactivated.json()["member"]["active"])
        self.assertGreaterEqual(deactivated.json()["revoked_sessions"], 1)

        self.assertEqual(self.client.get("/api/cases").status_code, 401)
        self.assertEqual(self.client.get("/api/cases", headers=self.headers(token)).status_code, 401)

    def test_token_rotation_revokes_the_previous_token(self):
        member, old = self.create_member(assignments=[{"case_id": self.case_a, "role": "viewer"}])
        rotated = self.client.post(f"/api/members/{member['id']}/tokens", headers=self.admin)
        self.assertEqual(rotated.status_code, 201, rotated.text)
        new = rotated.json()["token"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.client.get("/api/cases", headers=self.headers(old)).status_code, 401)
        self.assertEqual(self.client.get("/api/cases", headers=self.headers(new)).status_code, 200)

    def test_member_token_works_without_environment_tokens(self):
        member, token = self.create_member(assignments=[{"case_id": self.case_a, "role": "owner"}])
        with patch.dict("os.environ", {"LAW_REVIEW_API_TOKENS_JSON": ""}):
            listed = self.client.get("/api/cases", headers=self.headers(token))
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual([row["id"] for row in listed.json()], [self.case_a])

    def test_duplicate_or_reserved_names_are_rejected(self):
        self.create_member(name="律师甲")
        duplicate = self.client.post(
            "/api/members", headers=self.admin,
            json={"name": "律师甲", "permissions": ["view"]},
        )
        self.assertEqual(duplicate.status_code, 409)
        reserved = self.client.post(
            "/api/members", headers=self.admin,
            json={"name": "管理员", "permissions": ["view"]},
        )
        self.assertEqual(reserved.status_code, 409)


class CaseRoleTest(MemberTestCase):
    def test_reviewer_can_confirm_but_not_edit_content(self):
        reviewer, token = self.create_member(
            name="复核律师", permissions=["view", "approve", "export"],
            assignments=[{"case_id": self.case_a, "role": "reviewer"}],
        )
        created = self.client.post(
            f"/api/cases/{self.case_a}/evidence", headers=self.admin,
            json={"title": "借条原件", "category": "书证", "fact": "借款十万元", "quote": "十万元"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        evidence_id = created.json()["evidence"]["id"]

        edited = self.client.patch(
            f"/api/evidence/{evidence_id}", headers=self.headers(token), json={"fact": "复核律师改写的待证事实"}
        )
        self.assertEqual(edited.status_code, 403, edited.text)

        confirmed = self.client.patch(
            f"/api/evidence/{evidence_id}", headers=self.headers(token), json={"status": "已确认"}
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["evidence"]["approved_by"], "复核律师")

        # Reviewers may read and export, but creating records needs edit rights.
        self.assertEqual(self.client.get(f"/api/cases/{self.case_a}", headers=self.headers(token)).status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/api/cases/{self.case_a}/evidence", headers=self.headers(token),
                json={"title": "越权新增", "category": "书证", "fact": "事实"},
            ).status_code,
            403,
        )

    def test_assistant_cannot_approve(self):
        _, token = self.create_member(
            name="助理", permissions=["view", "edit"],
            assignments=[{"case_id": self.case_a, "role": "assistant"}],
        )
        created = self.client.post(
            f"/api/cases/{self.case_a}/evidence", headers=self.headers(token),
            json={"title": "助理录入证据", "category": "书证", "fact": "待证事实"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        evidence_id = created.json()["evidence"]["id"]
        denied = self.client.patch(
            f"/api/evidence/{evidence_id}", headers=self.headers(token), json={"status": "已确认"}
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "需要审批确认权限")

    def test_owner_can_assign_members_but_assistant_cannot(self):
        owner, owner_token = self.create_member(
            name="承办律师", assignments=[{"case_id": self.case_a, "role": "owner"}]
        )
        assistant, _ = self.create_member(
            name="助理", permissions=["view", "edit"],
            assignments=[{"case_id": self.case_a, "role": "assistant"}],
        )

        listed = self.client.get(f"/api/cases/{self.case_a}/members", headers=self.headers(owner_token))
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual({row["name"] for row in listed.json()["assignments"]}, {"承办律师", "助理"})

        promoted = self.client.put(
            f"/api/cases/{self.case_a}/members/{assistant['id']}",
            headers=self.headers(owner_token), json={"role": "reviewer"},
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(promoted.json()["previous_role"], "assistant")

        denied = self.client.put(
            f"/api/cases/{self.case_a}/members/{owner['id']}",
            headers=self.headers(self.create_member_token("助理2")),
            json={"role": "viewer"},
        )
        self.assertEqual(denied.status_code, 403)

    def create_member_token(self, name):
        _, token = self.create_member(name=name, permissions=["view", "edit"],
                                      assignments=[{"case_id": self.case_a, "role": "assistant"}])
        return token


class MemberAdministrationTest(MemberTestCase):
    def test_configured_principals_are_listed_read_only(self):
        listing = self.client.get("/api/members", headers=self.admin)
        self.assertEqual(listing.status_code, 200, listing.text)
        body = listing.json()
        self.assertEqual([row["name"] for row in body["configured"]], ["管理员"])
        self.assertEqual(body["members"], [])
        self.assertIn("owner", {row["key"] for row in body["roles"]})

    def test_member_admin_cannot_demote_or_disable_themselves(self):
        admin_member, token = self.create_member(name="目录管理员", admin=True)
        for payload in ({"active": False}, {"admin": False}):
            blocked = self.client.patch(
                f"/api/members/{admin_member['id']}", headers=self.headers(token), json=payload
            )
            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertIn("当前登录", blocked.json()["detail"])
        self.assertEqual(self.client.get("/api/members", headers=self.headers(token)).status_code, 200)

    def test_environment_admin_can_disable_a_directory_admin(self):
        admin_member, _ = self.create_member(name="备用管理员", admin=True)
        disabled = self.client.patch(
            f"/api/members/{admin_member['id']}", headers=self.admin, json={"active": False}
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["member"]["active"])

    def test_local_mode_rejects_member_administration(self):
        with patch.dict("os.environ", {"LAW_REVIEW_AUTH_MODE": "local"}):
            response = self.client.post(
                "/api/members", json={"name": "本地成员", "permissions": ["view"]}
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("本机模式", response.json()["detail"])

    def test_member_changes_are_audited_without_tokens(self):
        member, token = self.create_member(name="受审计成员", assignments=[{"case_id": self.case_a, "role": "viewer"}])
        self.client.patch(f"/api/members/{member['id']}", headers=self.admin, json={"permissions": ["view", "edit"]})
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT action, detail FROM audit_log WHERE action = '修改成员' ORDER BY id"
            ).fetchall()
        self.assertTrue(rows)
        detail = json.loads(rows[-1]["detail"])
        self.assertEqual(detail["actor"], "管理员")
        self.assertEqual(detail["fields"]["permissions"]["before"], "approve,edit,export,view")
        self.assertEqual(detail["fields"]["permissions"]["after"], "edit,view")
        self.assertNotIn(token, rows[-1]["detail"])

    def test_deactivated_member_cannot_be_assigned(self):
        member, _ = self.create_member(name="离职成员")
        self.client.patch(f"/api/members/{member['id']}", headers=self.admin, json={"active": False})
        response = self.client.put(
            f"/api/cases/{self.case_a}/members/{member['id']}", headers=self.admin, json={"role": "viewer"}
        )
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
