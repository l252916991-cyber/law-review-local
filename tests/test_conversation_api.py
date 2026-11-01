"""Conversation lifecycle over HTTP: list, read, archive (idempotent), delete.

Covers the handlers in app/routes/chat.py that previously ran only through the
browser suite, plus the read-only tool endpoint's error mapping.
"""
from __future__ import annotations

from contextlib import closing

from fastapi.testclient import TestClient

from tests.support import IsolatedDatabaseTestCase


class ConversationApiTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.db import now, transaction
        from app.main import app

        cls.client = TestClient(app)
        with transaction() as conn:
            cls.case_id = conn.execute(
                "INSERT INTO cases(title, status, created_at, updated_at) VALUES ('会话接口案件', '进行中', ?, ?)",
                (now(), now()),
            ).lastrowid
            document_id = conn.execute(
                """INSERT INTO documents(case_id, name, mime_type, pages, status, created_at, updated_at)
                   VALUES (?, '借款合同.txt', 'text/plain', 1, '已索引', ?, ?)""",
                (cls.case_id, now(), now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO pages(document_id, page_no, text, summary) VALUES (?, 1, ?, ?)",
                (document_id, "甲方向乙方借款人民币十万元，约定年利率百分之六。", "借款十万元"),
            )
        from app.db import sync_fts_index

        sync_fts_index()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def ask(self, question: str, conversation_id: int | None = None) -> dict:
        response = self.client.post(f"/api/cases/{self.case_id}/chat", json={
            "question": question, "conversation_id": conversation_id,
            "use_llm": False, "use_remote_embeddings": False,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def audit_actions(self) -> list[str]:
        from app.db import connect

        with closing(connect()) as conn:
            return [row[0] for row in conn.execute(
                "SELECT action FROM audit_log WHERE case_id=? ORDER BY id", (self.case_id,))]

    def test_listing_counts_messages_and_messages_carry_parsed_citations(self):
        first = self.ask("借款金额是多少？")
        conversation_id = first["conversation_id"]
        self.ask("利率是多少？", conversation_id)

        listing = self.client.get(f"/api/cases/{self.case_id}/conversations").json()
        row = next(item for item in listing if item["id"] == conversation_id)
        self.assertEqual(row["message_count"], 4)
        self.assertIs(row["archived"], False)

        messages = self.client.get(f"/api/conversations/{conversation_id}/messages").json()
        self.assertEqual([item["role"] for item in messages], ["user", "assistant", "user", "assistant"])
        for item in messages:
            self.assertIsInstance(item["citations"], list)
            self.assertNotIn("citations_json", item)
        self.assertTrue(messages[1]["citations"], "rule answer must cite the retrieved page")
        self.assertEqual(messages[1]["citations"][0]["page"], 1)

    def test_archive_is_idempotent_and_audited_once_per_call(self):
        conversation_id = self.ask("合同约定了什么？")["conversation_id"]
        first = self.client.post(f"/api/conversations/{conversation_id}/archive").json()
        second = self.client.post(f"/api/conversations/{conversation_id}/archive").json()
        self.assertTrue(first["archived"])
        # A repeated archive keeps the original timestamp and retention deadline.
        for key in ("archived_at", "archive_expires_at", "archived_by"):
            self.assertEqual(first[key], second[key])
        self.assertGreater(first["archive_expires_at"], 0)
        listing = self.client.get(f"/api/cases/{self.case_id}/conversations").json()
        self.assertIs(next(item for item in listing if item["id"] == conversation_id)["archived"], True)
        self.assertGreaterEqual(self.audit_actions().count("归档AI阅卷会话"), 2)

    def test_delete_removes_messages_and_later_reads_are_404(self):
        conversation_id = self.ask("谁是借款人？")["conversation_id"]
        deleted = self.client.delete(f"/api/conversations/{conversation_id}").json()
        self.assertEqual(deleted, {
            "conversation_id": conversation_id, "deleted": True, "messages_deleted": 2,
            "message": "会话及其消息已永久删除，无法恢复",
        })
        self.assertIn("永久删除AI阅卷会话", self.audit_actions())
        for method, path in (("get", f"/api/conversations/{conversation_id}/messages"),
                             ("post", f"/api/conversations/{conversation_id}/archive"),
                             ("delete", f"/api/conversations/{conversation_id}")):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(path).status_code, 404)

    def test_unknown_conversation_is_404_without_side_effects(self):
        before = len(self.audit_actions())
        self.assertEqual(self.client.get("/api/conversations/999999/messages").status_code, 404)
        self.assertEqual(self.client.post("/api/conversations/999999/archive").status_code, 404)
        self.assertEqual(self.client.delete("/api/conversations/999999").status_code, 404)
        self.assertEqual(len(self.audit_actions()), before)

    def test_readonly_tool_errors_map_to_client_errors(self):
        path = f"/api/cases/{self.case_id}/agent-tools/call"
        unknown = self.client.post(path, json={"tool": "delete_everything", "params": {}})
        self.assertEqual(unknown.status_code, 404)
        too_many = self.client.post(path, json={"tool": "search", "params": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}})
        self.assertEqual(too_many.status_code, 400)
        self.assertIn("上限", too_many.json()["detail"])

    def test_analyze_and_missing_case(self):
        response = self.client.post(f"/api/cases/{self.case_id}/analyze")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/cases/999999/conversations").status_code, 404)
