"""Case reports: draft → review → approval bound to a material snapshot."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from unittest.mock import patch

from tests.support import IsolatedDatabaseTestCase

TEST_DATA = tempfile.mkdtemp(prefix="lexvault-report-tests-")

from app.main import app  # noqa: E402
from app.db import db_scope, init_db, now, transaction  # noqa: E402


class ReportFlowTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def create_case(self, title="报告测试案"):
        response = self.client.post("/api/cases", json={"title": title})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create_evidence(self, case_id, **overrides):
        payload = {"title": "借条原件", "category": "书证", "fact": "借款十万元", "quote": "今借到人民币十万元"}
        payload.update(overrides)
        response = self.client.post(f"/api/cases/{case_id}/evidence", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["evidence"]

    @classmethod
    def template(cls):
        if getattr(cls, "_template_id", None):
            return cls._template_id
        response = cls.client.post(
            "/api/export-templates",
            json={"name": "报告测试模板", "blocks": [{"type": "case_summary", "title": "案件摘要"}]},
        )
        assert response.status_code == 201, response.text
        cls._template_id = response.json()["id"]
        return cls._template_id

    def create_report(self, case_id, **payload):
        response = self.client.post(f"/api/cases/{case_id}/reports", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["report"]

    def act(self, report_id, action, **extra):
        return self.client.patch(f"/api/reports/{report_id}", json={"action": action, **extra})

    def manifest_of(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            return json.loads(archive.read("清单.json"))

    def test_formal_export_requires_approved_report_and_tracks_material(self):
        case = self.create_case()
        evidence = self.create_evidence(case["id"], status="已确认")

        blocked = self.client.get(f"/api/cases/{case['id']}/export?template_id={self.template()}&final=true")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("先批准一份报告", blocked.text)

        report = self.create_report(case["id"], title="结案报告")
        self.assertEqual(report["status"], "草稿")
        self.assertEqual(report["version"], 1)
        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        approved = self.act(report["id"], "approve")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["report"]["status"], "已批准")
        self.assertEqual(approved.json()["report"]["approved_by"], "本机律师")

        manifest = self.manifest_of(self.client.get(f"/api/cases/{case['id']}/export?template_id={self.template()}&final=true"))
        self.assertTrue(manifest["finalized"])
        self.assertEqual(manifest["report"]["id"], report["id"])
        self.assertEqual(manifest["report"]["version"], 1)
        self.assertEqual(manifest["report"]["approved_by"], "本机律师")
        self.assertGreaterEqual(manifest["report"]["counts"]["evidence"], 1)

        # Editing confirmed evidence changes the snapshot and voids delivery.
        edited = self.client.patch(f"/api/evidence/{evidence['id']}", json={"fact": "借款金额更正为二十万元"})
        self.assertEqual(edited.status_code, 200, edited.text)
        stale = self.client.get(f"/api/cases/{case['id']}/export?template_id={self.template()}&final=true")
        self.assertEqual(stale.status_code, 400)
        self.assertIn("已变化", stale.text)

        listed = self.client.get(f"/api/cases/{case['id']}/reports").json()
        self.assertFalse(listed["deliverable"])
        self.assertTrue(listed["reports"][0]["stale"])

        second = self.create_report(case["id"], title="结案报告（修订）")
        self.assertEqual(second["version"], 2)
        self.act(second["id"], "submit")
        self.act(second["id"], "approve")
        manifest = self.manifest_of(self.client.get(f"/api/cases/{case['id']}/export?template_id={self.template()}&final=true"))
        self.assertEqual(manifest["report"]["version"], 2)

        listed = self.client.get(f"/api/cases/{case['id']}/reports").json()
        by_version = {row["version"]: row for row in listed["reports"]}
        self.assertEqual(by_version[1]["status"], "已作废")
        self.assertEqual(by_version[1]["superseded_by"], second["id"])
        self.assertFalse(by_version[1]["stale"])

    def test_return_requires_reason_and_allows_resubmit(self):
        case = self.create_case()
        report = self.create_report(case["id"])
        self.assertEqual(self.act(report["id"], "approve").status_code, 409)
        self.act(report["id"], "submit")
        rejected = self.act(report["id"], "return", note="  ")
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("原因", rejected.json()["detail"])
        returned = self.act(report["id"], "return", note="证据摘要与目录不一致")
        self.assertEqual(returned.status_code, 200, returned.text)
        body = returned.json()["report"]
        self.assertEqual(body["status"], "已退回")
        self.assertEqual(body["return_reason"], "证据摘要与目录不一致")
        self.assertEqual(body["events"][-1]["note"], "证据摘要与目录不一致")

        resubmitted = self.act(report["id"], "submit")
        self.assertEqual(resubmitted.status_code, 200, resubmitted.text)
        self.assertEqual(resubmitted.json()["report"]["status"], "待复核")
        self.assertEqual(resubmitted.json()["report"]["events"][-1]["from"], "已退回")

    def test_material_change_between_submit_and_approve_is_rejected(self):
        case = self.create_case()
        evidence = self.create_evidence(case["id"], status="已确认")
        report = self.create_report(case["id"])
        self.act(report["id"], "submit")
        self.client.patch(f"/api/evidence/{evidence['id']}", json={"credibility": "中"})
        blocked = self.act(report["id"], "approve")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("已变化", blocked.json()["detail"])
        self.assertEqual(self.client.get(f"/api/cases/{case['id']}/reports").json()["reports"][0]["status"], "待复核")

    def test_voided_report_blocks_formal_export(self):
        case = self.create_case()
        report = self.create_report(case["id"])
        self.act(report["id"], "submit")
        self.act(report["id"], "approve")
        self.assertEqual(self.client.get(f"/api/cases/{case['id']}/export?template_id={self.template()}&final=true").status_code, 200)
        voided = self.act(report["id"], "void", note="撤回重做")
        self.assertEqual(voided.status_code, 200, voided.text)
        self.assertEqual(voided.json()["report"]["status"], "已作废")
        blocked = self.client.get(f"/api/cases/{case['id']}/export?template_id={self.template()}&final=true")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("先批准一份报告", blocked.text)

    def test_preview_export_does_not_need_a_report(self):
        case = self.create_case()
        response = self.client.get(f"/api/cases/{case['id']}/export")
        self.assertEqual(response.status_code, 200, response.text)
        manifest = self.manifest_of(response)
        self.assertFalse(manifest["finalized"])
        self.assertIsNone(manifest["report"])


class ReportRoleTest(unittest.TestCase):
    """Role separation and case isolation for report transitions."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-report-roles-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        scope = db_scope(self.root / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case_a = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('报告甲案',?,?)", (now(), now())
            ).lastrowid
            self.case_b = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('报告乙案',?,?)", (now(), now())
            ).lastrowid
        self.env = patch.dict(
            "os.environ",
            {
                "LAW_REVIEW_AUTH_MODE": "token",
                "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
                "LAW_REVIEW_API_TOKENS_JSON": json.dumps({"b" * 40: {"name": "管理员", "admin": True}}),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.admin = {"Authorization": f"Bearer {'b' * 40}"}
        self.assistant, self.assistant_token = self.member("助理", "assistant", ["view", "edit"], self.case_a)
        self.reviewer, self.reviewer_token = self.member(
            "复核律师", "reviewer", ["view", "approve", "export"], self.case_a
        )
        self.outsider, self.outsider_token = self.member("外案律师", "viewer", ["view"], self.case_b)

    def member(self, name, role, permissions, case_id):
        response = self.client.post(
            "/api/members", headers=self.admin,
            json={"name": name, "permissions": permissions,
                  "assignments": [{"case_id": case_id, "role": role}]},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["member"], response.json()["token"]

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_roles_cannot_be_skipped(self):
        created = self.client.post(
            f"/api/cases/{self.case_a}/reports", headers=self.headers(self.assistant_token), json={}
        )
        self.assertEqual(created.status_code, 201, created.text)
        report_id = created.json()["report"]["id"]
        submitted = self.client.patch(
            f"/api/reports/{report_id}", headers=self.headers(self.assistant_token), json={"action": "submit"}
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)

        denied_approve = self.client.patch(
            f"/api/reports/{report_id}", headers=self.headers(self.assistant_token), json={"action": "approve"}
        )
        self.assertEqual(denied_approve.status_code, 403)

        denied_create = self.client.post(
            f"/api/cases/{self.case_a}/reports", headers=self.headers(self.reviewer_token), json={}
        )
        self.assertEqual(denied_create.status_code, 403)

        approved = self.client.patch(
            f"/api/reports/{report_id}", headers=self.headers(self.reviewer_token), json={"action": "approve"}
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["report"]["approved_by"], "复核律师")
        self.assertEqual(approved.json()["report"]["reviewed_by"], "复核律师")

    def test_report_is_case_scoped(self):
        report = self.client.post(
            f"/api/cases/{self.case_a}/reports", headers=self.admin, json={}
        ).json()["report"]
        self.assertEqual(
            self.client.get(f"/api/cases/{self.case_a}/reports", headers=self.headers(self.outsider_token)).status_code,
            404,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/reports/{report['id']}", headers=self.headers(self.outsider_token), json={"action": "submit"}
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
