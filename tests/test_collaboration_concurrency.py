import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.support import IsolatedDatabaseTestCase

TEST_DATA = tempfile.mkdtemp(prefix="lexvault-collab-tests-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect, db_scope, init_db, now, transaction  # noqa: E402
from app.main import app  # noqa: E402


class CollaborationTestCase(IsolatedDatabaseTestCase):
    def create_case(self, title="协作并发案件", **overrides):
        payload = {"title": title, "case_no": "(2026)协1号", **overrides}
        response = self.client.post("/api/cases", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create_evidence(self, case_id, **overrides):
        payload = {"title": "借条原件", "category": "书证", "fact": "借款金额十万元", "quote": "今借到人民币十万元"}
        payload.update(overrides)
        response = self.client.post(f"/api/cases/{case_id}/evidence", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["evidence"]

    def upload_text(self, case_id, name="借条.txt", text="借条：今借到人民币十万元整，借款人张三。"):
        from app.services import index_upload
        return index_upload(case_id, name, text.encode("utf-8"), "text/plain")

    def audit_rows(self, case_id, action):
        with closing(connect()) as conn:
            return [
                row["detail"]
                for row in conn.execute(
                    "SELECT detail FROM audit_log WHERE case_id=? AND action=? ORDER BY id", (case_id, action)
                ).fetchall()
            ]


class EvidenceConcurrencyTest(CollaborationTestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_stale_version_is_rejected_instead_of_overwriting(self):
        case_id = self.create_case()["id"]
        evidence = self.create_evidence(case_id)
        self.assertEqual(evidence["version"], 1)

        first = self.client.patch(
            f"/api/evidence/{evidence['id']}",
            json={"fact": "借款金额十万元（甲修改）", "expected_version": 1},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["evidence"]["version"], 2)

        # 乙仍拿着版本 1 保存：必须失败，不能覆盖甲刚提交的内容。
        stale = self.client.patch(
            f"/api/evidence/{evidence['id']}",
            json={"fact": "借款金额二十万元（乙修改）", "expected_version": 1},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("当前版本 2", stale.json()["detail"])

        current = self.client.get(f"/api/cases/{case_id}/evidence").json()["evidence"][0]
        self.assertEqual(current["fact"], "借款金额十万元（甲修改）")
        self.assertEqual(current["version"], 2)

    def test_rejected_save_leaves_no_audit_row_and_no_version_bump(self):
        case_id = self.create_case()["id"]
        evidence = self.create_evidence(case_id)
        before_rows = len(self.audit_rows(case_id, "修改证据"))
        rejected = self.client.patch(
            f"/api/evidence/{evidence['id']}",
            json={"fact": "冲突写入", "expected_version": 99},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(len(self.audit_rows(case_id, "修改证据")), before_rows)
        with closing(connect()) as conn:
            row = conn.execute("SELECT fact, version FROM evidence WHERE id=?", (evidence["id"],)).fetchone()
        self.assertEqual((row["fact"], row["version"]), ("借款金额十万元", 1))

    def test_edit_audit_keeps_actor_and_before_after_values(self):
        case_id = self.create_case()["id"]
        evidence = self.create_evidence(case_id, fact="原始待证事实")
        updated = self.client.patch(
            f"/api/evidence/{evidence['id']}",
            json={"title": "借条原件（更正）", "fact": "更正后的待证事实", "expected_version": 1},
        )
        self.assertEqual(updated.status_code, 200, updated.text)

        details = [json.loads(detail) for detail in self.audit_rows(case_id, "修改证据")]
        self.assertEqual(len(details), 1)
        detail = details[0]
        self.assertEqual(detail["actor"], "本机律师")
        self.assertEqual(detail["version"], {"from": 1, "to": 2})
        self.assertEqual(detail["fields"]["title"], {"before": "借条原件", "after": "借条原件（更正）"})
        self.assertEqual(detail["fields"]["fact"], {"before": "原始待证事实", "after": "更正后的待证事实"})
        self.assertIn(f"证据#{evidence['id']}", detail["target"])

    def test_edit_without_expected_version_stays_compatible(self):
        case_id = self.create_case()["id"]
        evidence = self.create_evidence(case_id)
        response = self.client.patch(f"/api/evidence/{evidence['id']}", json={"credibility": "中"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["evidence"]["version"], 2)


class CaseUpdateTest(CollaborationTestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_case_info_can_be_corrected_with_version_and_audit(self):
        case = self.create_case(title="王某合同纠纷案", case_no="(2026)民初1号")
        response = self.client.patch(
            f"/api/cases/{case['id']}",
            json={"case_no": "(2026)民初2号", "client_name": "王某", "expected_version": case["version"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["case_no"], "(2026)民初2号")
        self.assertEqual(body["client_name"], "王某")
        self.assertEqual(body["version"], 2)

        detail = json.loads(self.audit_rows(case["id"], "修改案件信息")[0])
        self.assertEqual(detail["actor"], "本机律师")
        self.assertEqual(detail["fields"]["case_no"], {"before": "(2026)民初1号", "after": "(2026)民初2号"})
        self.assertEqual(detail["fields"]["client_name"], {"before": "", "after": "王某"})

    def test_stale_case_version_is_rejected(self):
        case = self.create_case()
        first = self.client.patch(
            f"/api/cases/{case['id']}", json={"title": "协作并发案件（甲）", "expected_version": 1}
        )
        self.assertEqual(first.status_code, 200, first.text)
        stale = self.client.patch(
            f"/api/cases/{case['id']}", json={"title": "协作并发案件（乙）", "expected_version": 1}
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.client.get(f"/api/cases/{case['id']}").json()["title"], "协作并发案件（甲）")

    def test_empty_update_is_rejected(self):
        case = self.create_case()
        self.assertEqual(self.client.patch(f"/api/cases/{case['id']}", json={}).status_code, 400)


class MaterialCorrectionTest(CollaborationTestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_directory_correction_records_before_after_and_version(self):
        case = self.create_case()
        document = self.upload_text(case["id"])
        response = self.client.patch(
            f"/api/documents/{document['id']}/directory",
            json={"doc_type": "书证", "summary": "人工校对摘要", "expected_version": document["version"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], document["version"] + 1)
        self.assertEqual(response.json()["summary"], "人工校对摘要")

        detail = json.loads(self.audit_rows(case["id"], "校准目录")[0])
        self.assertEqual(detail["fields"]["summary"], {"before": document["summary"], "after": "人工校对摘要"})
        self.assertEqual(detail["version"], {"from": document["version"], "to": document["version"] + 1})

        stale = self.client.patch(
            f"/api/documents/{document['id']}/directory",
            json={"summary": "另一个人的修改", "expected_version": document["version"]},
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_page_correction_invalidates_only_broken_quote(self):
        case = self.create_case()
        document = self.upload_text(case["id"])
        broken = self.create_evidence(
            case["id"], title="借款金额证据", fact="借款十万元", quote="今借到人民币十万元整",
            source_document_id=document["id"], source_page_start=1, source_page_end=1, status="已确认",
        )
        kept = self.create_evidence(
            case["id"], title="借款人证据", fact="借款人为张三", quote="借款人张三",
            source_document_id=document["id"], source_page_start=1, source_page_end=1, status="已确认",
        )
        self.assertEqual(broken["approved_by"], "本机律师")

        response = self.client.patch(
            f"/api/documents/{document['id']}/pages/1",
            json={"text": "借条：今借到人民币拾万元整，借款人张三。", "expected_version": document["version"],
                  "reason": "OCR 误识“十”为“拾”"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["changed"])
        self.assertEqual([item["id"] for item in body["invalidated"]], [broken["id"]])
        self.assertEqual(body["version"], document["version"] + 1)
        self.assertEqual(body["page"]["text"], "借条：今借到人民币拾万元整，借款人张三。")

        evidence = {item["id"]: item for item in self.client.get(f"/api/cases/{case['id']}/evidence").json()["evidence"]}
        self.assertEqual(evidence[broken["id"]]["status"], "待复核")
        self.assertIsNone(evidence[broken["id"]]["approved_by"])
        self.assertEqual(evidence[kept["id"]]["status"], "已确认")
        self.assertEqual(evidence[kept["id"]]["approved_by"], "本机律师")
        self.assertEqual(len(self.audit_rows(case["id"], "审批失效")), 1)
        self.assertEqual(len(self.audit_rows(case["id"], "纠错原文")), 1)

    def test_page_correction_rejects_stale_version(self):
        case = self.create_case()
        document = self.upload_text(case["id"])
        stale = self.client.patch(
            f"/api/documents/{document['id']}/pages/1",
            json={"text": "别人的修改", "expected_version": 99},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        page = self.client.get(f"/api/documents/{document['id']}/pages/1").json()
        self.assertIn("今借到人民币十万元整", page["text"])

    def test_simultaneous_page_corrections_have_one_winner_and_one_conflict(self):
        from app import main

        case = self.create_case()
        document = self.upload_text(case["id"])
        evidence = self.create_evidence(
            case["id"], source_document_id=document["id"], quote="今借到人民币十万元整", status="已确认",
        )
        get_document = main.get_document
        barrier = threading.Barrier(2)
        local = threading.local()

        def read_together(identifier):
            result = get_document(identifier)
            if not getattr(local, "initial_read", False):
                local.initial_read = True
                barrier.wait(timeout=10)
            return result

        def save(value):
            return self.client.patch(
                f"/api/documents/{document['id']}/pages/1",
                json={"text": value, "expected_version": document["version"]},
            )

        with patch.object(main, "get_document", side_effect=read_together), ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(save, ["合成原文修改甲", "合成原文修改乙"]))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        winner = next(response.json() for response in responses if response.status_code == 200)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT version FROM documents WHERE id=?", (document["id"],)).fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT text FROM pages WHERE document_id=?", (document["id"],)).fetchone()[0],
                             winner["page"]["text"])
            row = conn.execute("SELECT status,version FROM evidence WHERE id=?", (evidence["id"],)).fetchone()
            self.assertEqual(tuple(row), ("待复核", 2))
        self.assertEqual(len(self.audit_rows(case["id"], "纠错原文")), 1)
        self.assertEqual(len(self.audit_rows(case["id"], "审批失效")), 1)

    def test_missing_page_is_reported(self):
        case = self.create_case()
        document = self.upload_text(case["id"])
        response = self.client.patch(f"/api/documents/{document['id']}/pages/9", json={"text": "无此页"})
        self.assertEqual(response.status_code, 404)


class MultiPrincipalEditingTest(unittest.TestCase):
    """两名律师同时操作同一案件时的覆盖与归属。"""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-collab-token-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        scope = db_scope(self.root / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('协作案',?,?)", (now(), now())
            ).lastrowid
        self.jia, self.yi = "a" * 40, "b" * 40
        env = patch.dict(os.environ, {
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({
                self.jia: {"name": "律师甲", "case_ids": [self.case]},
                self.yi: {"name": "律师乙", "case_ids": [self.case]},
            }),
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_second_lawyer_gets_a_conflict_and_approval_is_attributed(self):
        jia = {"Authorization": f"Bearer {self.jia}"}
        yi = {"Authorization": f"Bearer {self.yi}"}
        created = self.client.post(
            f"/api/cases/{self.case}/evidence", headers=jia,
            json={"title": "借条原件", "category": "书证", "fact": "借款十万元", "quote": "今借到人民币十万元"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        evidence = created.json()["evidence"]

        first = self.client.patch(
            f"/api/evidence/{evidence['id']}", headers=jia,
            json={"fact": "借款十万元（甲核对）", "expected_version": 1},
        )
        self.assertEqual(first.status_code, 200, first.text)
        stale = self.client.patch(
            f"/api/evidence/{evidence['id']}", headers=yi,
            json={"fact": "借款十万元（乙核对）", "expected_version": 1},
        )
        self.assertEqual(stale.status_code, 409, stale.text)

        confirmed = self.client.patch(
            f"/api/evidence/{evidence['id']}", headers=yi,
            json={"status": "已确认", "expected_version": 2},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["evidence"]["approved_by"], "律师乙")

        with closing(connect()) as conn:
            details = [
                json.loads(row["detail"]) for row in conn.execute(
                    "SELECT detail FROM audit_log WHERE case_id=? AND action='修改证据' ORDER BY id", (self.case,)
                ).fetchall()
            ]
        self.assertEqual(details[0]["actor"], "律师甲")
        self.assertEqual(details[1]["actor"], "律师乙")
        self.assertEqual(details[1]["fields"]["approved_by"]["after"], "律师乙")


if __name__ == "__main__":
    unittest.main()
