"""Information walls: nobody outside the wall can read any page of a walled case.

Case 1 (the seeded demo case) is walled; every page, its title and one piece of
evidence carry ``MARKER``. Two outsiders are tested: the firm administrator
(administrators are not exempt) and a lawyer who is still assigned to the case
but was screened out. A sweep calls every GET endpoint with the walled case's
ids, and asserts the marker never appears. On PostgreSQL a second sweep runs
with the application check switched off, to show the database's row-level
security holds on its own.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import zipfile
from contextlib import closing
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.support import IsolatedDatabaseTestCase

MARKER = "墙内机密7F3A"
ADMIN_TOKEN = "compliance-admin-token-" + "x" * 24


class CaseWallTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.db import connect, init_db, sync_fts_index, system_access
        from app.main import app

        env = patch.dict(os.environ, {
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({ADMIN_TOKEN: {"name": "合规管理员", "admin": True}}),
        })
        env.start()
        cls.addClassCleanup(env.stop)
        init_db(seed=True)
        with system_access(), closing(connect()) as conn, conn:
            conn.execute("UPDATE cases SET title = title || ? WHERE id = 1", (MARKER,))
            conn.execute("UPDATE pages SET text = text || ? WHERE document_id IN (SELECT id FROM documents WHERE case_id = 1)",
                         (MARKER,))
        sync_fts_index()
        cls.app = app
        cls.client = TestClient(app)
        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        cls.admin = admin
        open_case = cls.client.post("/api/cases", json={"title": "公开案件"}, headers=admin)
        assert open_case.status_code == 201, open_case.text
        cls.open_case = open_case.json()["id"]

        def member(name: str, role: str) -> tuple[int, dict]:
            created = cls.client.post("/api/members", headers=admin, json={
                "name": name, "assignments": [{"case_id": 1, "role": role}, {"case_id": cls.open_case, "role": role}]})
            assert created.status_code == 201, created.text
            return created.json()["member"]["id"], {"Authorization": f"Bearer {created.json()['token']}"}

        cls.insider_id, cls.insider = member("墙内律师", "owner")
        cls.screened_id, cls.screened = member("墙外律师", "assistant")
        chat = cls.client.post("/api/cases/1/chat", headers=cls.insider,
                               json={"question": "涉案账户累计流入多少万元", "use_llm": False, "use_remote_embeddings": False})
        assert chat.status_code == 200, chat.text
        cls.conversation = chat.json()["conversation_id"]
        evidence = cls.client.post("/api/cases/1/evidence", headers=cls.insider, json={
            "title": f"证据{MARKER}", "category": "书证", "fact": MARKER, "quote": MARKER, "status": "待复核"})
        assert evidence.status_code == 201, evidence.text
        cls.evidence = evidence.json()["evidence"]["id"]
        with system_access(), closing(connect()) as conn:
            cls.document = conn.execute("SELECT MIN(id) FROM documents WHERE case_id = 1").fetchone()[0]
            run = conn.execute("SELECT MIN(id) FROM agent_runs WHERE case_id = 1").fetchone()[0]
            report = conn.execute("SELECT MIN(id) FROM case_reports WHERE case_id = 1").fetchone()[0]
            task = conn.execute("SELECT MIN(id) FROM review_tasks WHERE case_id = 1").fetchone()[0]
        cls.ids = {"case_id": 1, "document_id": cls.document, "page_no": 1, "evidence_id": cls.evidence,
                   "conversation_id": cls.conversation, "run_id": run or 1, "report_id": report or 1,
                   "task_id": task or 1, "member_id": cls.screened_id, "template_id": 1, "batch_id": 1,
                   "lifecycle_status": "archived", "job_id": "missing", "annotation_id": 1}
        wall = cls.client.put("/api/case-walls/1", headers=admin, json={
            "reason": "对方当事人由本所另案代理", "insiders": ["墙内律师"]})
        assert wall.status_code == 200, wall.text

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    # -- helpers ------------------------------------------------------------------
    def sweep(self, client: TestClient, headers: dict, *, refused: bool = True) -> list[tuple[str, int]]:
        """GET every API path with the walled case's ids; return what each answered.

        ``refused``: case paths must also answer with an error. With only the
        database layer active a list may legitimately come back empty instead.
        """
        answered = []
        for path, operations in self.app.openapi()["paths"].items():
            if "get" not in operations or not path.startswith("/api"):
                continue
            url = re.sub(r"{(\w+)}", lambda match: str(self.ids.get(match.group(1), 1)), path)
            response = client.get(url, headers=headers, params={"q": MARKER})
            body = response.content
            if response.headers.get("content-type", "").startswith("application/zip"):
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    body = b"".join(archive.read(member) for member in archive.namelist())
            with self.subTest(path=path):
                self.assertNotIn(MARKER, body.decode("utf-8", "replace"), f"{url} leaked walled content")
                if refused and ("{case_id}" in path or any(f"{{{key}}}" in path for key in (
                        "document_id", "evidence_id", "conversation_id"))):
                    self.assertGreaterEqual(response.status_code, 400, url)
            answered.append((path, response.status_code))
        return answered

    def audit_actions(self) -> list[str]:
        from app.db import connect, system_access

        with system_access(), closing(connect()) as conn:
            return [row[0] for row in conn.execute("SELECT action FROM audit_log WHERE case_id = 1 ORDER BY id")]

    # -- tests ----------------------------------------------------------------------
    def test_insider_keeps_full_access(self):
        case = self.client.get("/api/cases/1", headers=self.insider)
        self.assertEqual(case.status_code, 200)
        self.assertIn(MARKER, case.json()["title"])
        page = self.client.get(f"/api/documents/{self.document}/pages/1", headers=self.insider)
        self.assertIn(MARKER, page.json()["text"])
        self.assertIn(1, [item["id"] for item in self.client.get("/api/cases", headers=self.insider).json()])

    def test_administrator_outside_the_wall_reads_nothing(self):
        answered = dict(self.sweep(self.client, self.admin))
        self.assertGreater(len(answered), 30)
        self.assertEqual(answered["/api/cases/{case_id}"], 404)
        self.assertNotIn(1, [item["id"] for item in self.client.get("/api/cases", headers=self.admin).json()])

    def test_screened_lawyer_still_assigned_to_the_case_reads_nothing(self):
        self.sweep(self.client, self.screened)
        listed = [item["id"] for item in self.client.get("/api/cases", headers=self.screened).json()]
        self.assertEqual(listed, [self.open_case])

    def test_outsiders_cannot_act_on_the_walled_case(self):
        for headers in (self.admin, self.screened):
            for method, path, body in (
                ("post", "/api/cases/1/chat", {"question": "借款金额", "use_llm": False, "use_remote_embeddings": False}),
                ("post", "/api/cases/1/agent-tools/call", {"tool": "search", "params": {"query": "借款"}}),
                ("post", "/api/cases/1/evidence", {"title": "x", "category": "书证", "fact": "x", "quote": "x"}),
                ("patch", f"/api/evidence/{self.evidence}", {"title": "改写"}),
            ):
                with self.subTest(method=method, path=path):
                    response = getattr(self.client, method)(path, headers=headers, json=body)
                    self.assertIn(response.status_code, (403, 404), response.text)
                    self.assertNotIn(MARKER, response.text)

    def test_cross_case_views_omit_the_walled_case(self):
        dashboard = self.client.get("/api/dashboard", headers=self.admin).json()
        self.assertNotIn(MARKER, json.dumps(dashboard, ensure_ascii=False))
        self.assertTrue(all(item.get("case_id") != 1 for item in dashboard["recent"]))
        members = self.client.get("/api/members", headers=self.admin).json()["members"]
        screened = next(item for item in members if item["name"] == "墙外律师")
        self.assertEqual(screened["case_ids"], [self.open_case])
        self.assertNotIn(MARKER, json.dumps(members, ensure_ascii=False))
        walls = self.client.get("/api/case-walls", headers=self.admin).json()
        self.assertEqual([wall["case_id"] for wall in walls], [1])
        self.assertEqual(walls[0]["insiders"], ["墙内律师"])
        self.assertNotIn(MARKER, json.dumps(walls, ensure_ascii=False), "wall management must not show the title")

    def test_break_glass_is_reasoned_time_limited_and_audited(self):
        from app.db import connect, system_access, transaction

        self.assertEqual(self.client.post("/api/case-walls/1/break-glass", headers=self.admin,
                                          json={"reason": "太短"}).status_code, 422)
        self.assertEqual(self.client.post("/api/case-walls/1/break-glass", headers=self.screened,
                                          json={"reason": "我想看看这个案件的材料内容"}).status_code, 403)
        granted = self.client.post("/api/case-walls/1/break-glass", headers=self.admin,
                                   json={"reason": "客户投诉核查，需要核对材料归档情况"})
        self.assertEqual(granted.status_code, 201, granted.text)
        try:
            self.assertEqual(self.client.get("/api/cases/1", headers=self.admin).status_code, 200)
            self.assertIn("隔离墙破窗访问", self.audit_actions())
            with system_access(), closing(connect()) as conn:
                events = [row[0] for row in conn.execute(
                    "SELECT outcome FROM security_events WHERE event_type = 'wall_break_glass' AND case_id = 1")]
            self.assertIn("granted", events)
            with system_access(), transaction() as conn:
                conn.execute("UPDATE case_wall_grants SET expires_at = 0 WHERE case_id = 1")
            self.assertEqual(self.client.get("/api/cases/1", headers=self.admin).status_code, 404)
        finally:
            with system_access(), transaction() as conn:
                conn.execute("DELETE FROM case_wall_grants WHERE case_id = 1")
        self.assertEqual(self.client.post("/api/case-walls/1/break-glass", headers=self.insider,
                                          json={"reason": "墙内的人不需要破窗访问"}).status_code, 403)

    def test_only_administrators_manage_walls_and_changes_are_audited(self):
        body = {"reason": "测试调整名单", "insiders": ["墙内律师"]}
        self.assertEqual(self.client.put("/api/case-walls/1", headers=self.insider, json=body).status_code, 403)
        self.assertEqual(self.client.put("/api/case-walls/999", headers=self.admin, json=body).status_code, 404)
        self.assertEqual(self.client.put("/api/case-walls/1", headers=self.admin,
                                         json={"reason": "空名单", "insiders": ["  "]}).status_code, 422)
        self.assertEqual(self.client.put("/api/case-walls/1", headers=self.admin, json=body).status_code, 200)
        self.assertIn("调整隔离墙", self.audit_actions())

    def test_removing_the_wall_restores_ordinary_access(self):
        self.assertEqual(self.client.delete("/api/case-walls/1", headers=self.admin,
                                            params={"reason": "关联案件已结案"}).status_code, 200)
        try:
            self.assertEqual(self.client.get("/api/cases/1", headers=self.admin).status_code, 200)
            self.assertEqual(self.client.get("/api/cases/1", headers=self.screened).status_code, 200)
            self.assertIn("撤除隔离墙", self.audit_actions())
        finally:
            restored = self.client.put("/api/case-walls/1", headers=self.admin,
                                       json={"reason": "对方当事人由本所另案代理", "insiders": ["墙内律师"]})
            self.assertEqual(restored.status_code, 200)

    def test_walls_cannot_be_set_in_local_single_user_mode_and_mcp_refuses_walled_cases(self):
        from app import mcp_server

        with patch.dict(os.environ, {"LAW_REVIEW_AUTH_MODE": "local"}):
            response = self.client.put("/api/case-walls/2", json={"reason": "本机演示设墙", "insiders": ["本机律师"]})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(mcp_server.main(["--case-id", "1"]), 2)

    # -- PostgreSQL: the database enforces the wall by itself ---------------------
    def test_row_level_security_holds_without_the_application_check(self):
        from app.db import backend_name

        if backend_name() != "postgresql":
            self.skipTest("row-level security exists only on PostgreSQL")
        nothing_hidden = patch.multiple("app.case_walls", hidden_case_ids=lambda: frozenset())
        with nothing_hidden, patch("app.routes.cases.hidden_case_ids", lambda: frozenset()), \
                patch("app.routes.system.hidden_case_ids", lambda: frozenset()), \
                TestClient(self.app, raise_server_exceptions=False) as client:
            self.sweep(client, self.admin, refused=False)
            self.sweep(client, self.screened, refused=False)

    def test_database_rows_follow_the_wall(self):
        from app.db import backend_name, connect, now
        from app.security import Principal, _principal

        if backend_name() != "postgresql":
            self.skipTest("row-level security exists only on PostgreSQL")

        def visible_pages(name: str | None) -> int:
            token = _principal.set(Principal(name, True) if name else None)
            try:
                with closing(connect()) as conn:
                    return conn.execute("SELECT COUNT(*) FROM pages p JOIN documents d ON d.id = p.document_id "
                                        "WHERE d.case_id = 1").fetchone()[0]
            finally:
                _principal.reset(token)

        self.assertEqual(visible_pages("合规管理员"), 0)
        self.assertGreater(visible_pages("墙内律师"), 0)
        self.assertEqual(visible_pages(None), visible_pages("墙内律师"))  # system context
        token = _principal.set(Principal("合规管理员", True))
        try:
            with closing(connect()) as conn, pytest.raises(sqlite3.DatabaseError, match="row-level security"):
                conn.execute("INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (1, 'x', 'x', ?)",
                             (now(),))
        finally:
            _principal.reset(token)
