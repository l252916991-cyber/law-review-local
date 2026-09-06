"""Actual authentication, case isolation, CSRF and trusted identity checks."""
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import connect, db_scope, init_db, now, transaction
from app.main import app
from app.security import configured_tokens


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-access-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        scope = db_scope(self.root / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.cases = [conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", (title, now(), now())).lastrowid for title in ("公开给甲", "仅乙可见")]
            self.doc = conn.execute("INSERT INTO documents(case_id,name,created_at,updated_at) VALUES (?,?,?,?)", (self.cases[1], "乙的卷宗", now(), now())).lastrowid
            self.evidence = conn.execute("INSERT INTO evidence(case_id,title,created_at) VALUES (?,?,?)", (self.cases[1], "乙的证据", now())).lastrowid
            self.conversation = conn.execute("INSERT INTO conversations(case_id,user_name,title,created_at) VALUES (?,?,?,?)", (self.cases[1], "乙", "秘密", now())).lastrowid
            self.run = conn.execute("INSERT INTO agent_runs(case_id,question,route,status,created_at) VALUES (?,?,?,'failed',?)", (self.cases[1], "问题", "事实检索", now())).lastrowid
        self.token = "a" * 40
        self.admin_token = "b" * 40
        env = patch.dict(os.environ, {"LAW_REVIEW_AUTH_MODE": "token", "LAW_REVIEW_ALLOWED_HOSTS": "testserver", "LAW_REVIEW_API_TOKENS_JSON": json.dumps({self.token: {"name": "律师甲", "case_ids": [self.cases[0]]}, self.admin_token: {"name": "管理员", "admin": True}})})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_anonymous_denied_and_invalid_token_does_not_leak(self):
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            result = self.client.get("/api/cases", headers=headers)
            self.assertEqual(result.status_code, 401)
            self.assertNotIn("仅乙", result.text)

    def test_list_filtered_and_all_indirect_case_resources_denied(self):
        result = self.client.get("/api/cases", headers=self.headers)
        self.assertEqual([row["id"] for row in result.json()], [self.cases[0]])
        for path in (f"/api/cases/{self.cases[1]}", f"/api/documents/{self.doc}/pages/1", f"/api/documents/{self.doc}/file", f"/api/evidence/{self.evidence}/annotations", f"/api/conversations/{self.conversation}/messages", f"/api/agent-runs/{self.run}"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/evidence/{self.evidence}", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post(f"/api/agent-runs/{self.run}/resume", headers=self.headers).status_code, 404)

    def test_scoped_member_cannot_create_case_or_read_global_dashboard(self):
        self.assertEqual(self.client.post("/api/cases", json={"title": "越权新建"}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get("/api/dashboard", headers=self.headers).status_code, 403)
        admin = {"Authorization": f"Bearer {self.admin_token}"}
        self.assertEqual(self.client.post("/api/cases", json={"title": "管理员新建"}, headers=admin).status_code, 201)

    def test_cookie_login_logout_and_csrf_host_rejection(self):
        response = self.client.post("/api/auth/session", json={"token": self.token})
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(self.client.get("/api/cases").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/session", json={"token": self.token}, headers={"Origin": "https://attacker.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/cases", headers={"Host": "attacker.example"}).status_code, 400)
        self.assertEqual(self.client.delete("/api/auth/session").status_code, 200)
        self.assertEqual(self.client.get("/api/cases").status_code, 401)

    def test_session_expiry_revocation_rotation_and_replay(self):
        from app import security
        self.client.post('/api/auth/session', json={'token': self.token})
        session = self.client.cookies.get('lexvault_session')
        self.assertNotEqual(session, self.token)
        self.assertNotIn(self.token, repr(security._sessions))
        self.assertEqual(self.client.get('/api/cases', headers={'Authorization': f'Bearer {session}'}).status_code, 401)
        self.client.delete('/api/auth/session')
        self.client.cookies.set('lexvault_session', session)
        self.assertEqual(self.client.get('/api/cases').status_code, 401)
        self.client.cookies.clear()
        self.client.post('/api/auth/session', json={'token': self.token})
        with patch('app.security.SESSION_TTL', 0):
            self.client.post('/api/auth/session', json={'token': self.token})
        self.assertEqual(self.client.get('/api/cases').status_code, 401)
        self.client.post('/api/auth/session', json={'token': self.token})
        with patch.dict(os.environ, {'LAW_REVIEW_API_TOKENS_JSON': json.dumps({self.admin_token: {'name': '管理员', 'admin': True}})}):
            self.assertEqual(self.client.get('/api/cases').status_code, 401)
        # A configured bearer credential is never accepted as a browser session.
        self.client.cookies.clear()
        self.client.cookies.set('lexvault_session', self.token)
        self.assertEqual(self.client.get('/api/cases').status_code, 401)

    def test_request_user_name_cannot_forge_audit_identity(self):
        with patch("app.services.call_local_llm", side_effect=AssertionError("no model")):
            response = self.client.post(f"/api/cases/{self.cases[0]}/chat", headers=self.headers, json={"question": "案件有多少份卷宗？", "user_name": "伪造管理员", "use_llm": False})
        self.assertEqual(response.status_code, 200, response.text)
        with closing(connect()) as conn:
            user = conn.execute("SELECT user_name FROM conversations WHERE id=?", (response.json()["conversation_id"],)).fetchone()[0]
        self.assertEqual(user, "律师甲")

    def test_security_headers_and_no_cache(self):
        response = self.client.get("/api/cases", headers=self.headers)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_security_headers_cover_middleware_errors(self):
        cases = [
            (self.client.get("/api/cases", headers={}), 401),
            (self.client.get("/api/cases", headers={"Host": "attacker.example"}), 400),
            (self.client.get("/api/dashboard", headers=self.headers), 403),
            (self.client.get("/api/cases/999999", headers=self.headers), 404),
        ]
        for response, status in cases:
            with self.subTest(status=status):
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_bad_configuration_error_has_security_headers(self):
        with patch.dict(os.environ, {"LAW_REVIEW_API_TOKENS_JSON": "{}"}):
            response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_active_upload_is_rejected_and_legacy_file_is_sandboxed(self):
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        upload = self.client.post(f"/api/cases/{self.cases[0]}/documents", headers=headers,
                                  files=[("files", ("attack.html", b"<script>fetch('/api/cases')</script>", "text/html"))])
        self.assertEqual(upload.status_code, 201)
        self.assertEqual(upload.json()["documents"], [])
        self.assertEqual(len(upload.json()["failures"]), 1)

        path = self.root / f"{self._testMethodName}.html"
        path.write_text("<script>document.title='owned'</script>", encoding="utf-8")
        with transaction() as conn:
            doc_id = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                                  (self.cases[0], "legacy.html", str(path), "text/html", now(), now())).lastrowid
        response = self.client.get(f"/api/documents/{doc_id}/file", headers=headers)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.headers["content-security-policy"], "sandbox; default-src 'none'")

    def test_document_download_rejects_paths_outside_data_dir(self):
        headers = {"Authorization": f"Bearer {self.admin_token}"}
        outside = self.root.parent / "lexvault-outside-secret.txt"
        outside.write_text("外部机密内容", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.root / "escape-link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("当前环境不支持符号链接")
        with transaction() as conn:
            direct_id = conn.execute(
                "INSERT INTO documents(case_id,name,stored_path,mime_type,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (self.cases[0], "outside.txt", str(outside), "text/plain", now(), now())).lastrowid
            link_id = conn.execute(
                "INSERT INTO documents(case_id,name,stored_path,mime_type,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (self.cases[0], "escape.txt", str(link), "text/plain", now(), now())).lastrowid
        for doc_id in (direct_id, link_id):
            with self.subTest(doc_id=doc_id):
                response = self.client.get(f"/api/documents/{doc_id}/file", headers=headers)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("外部机密内容", response.text)

    def test_security_events_record_denials_auth_and_downloads(self):
        from app.db import connect
        self.client.get("/api/cases")  # 401 anonymous
        self.client.get("/api/dashboard", headers=self.headers)  # 403 scoped member
        self.client.post("/api/auth/session", json={"token": "wrong-token"})  # failed login
        self.client.post("/api/auth/session", json={"token": self.token})  # success
        self.client.delete("/api/auth/session")
        self.client.get("/api/cases", headers={"Host": "attacker.example"})  # 400
        with closing(connect()) as conn:
            events = [dict(row) for row in conn.execute(
                "SELECT event_type,outcome,actor,detail,request_path FROM security_events ORDER BY id")]
        kinds = {(e["event_type"], e["outcome"]) for e in events}
        self.assertIn(("access", "denied"), kinds)
        self.assertIn(("auth", "login_failed"), kinds)
        self.assertIn(("auth", "login_success"), kinds)
        self.assertIn(("auth", "logout"), kinds)
        self.assertTrue(any(e["request_path"] == "/api/cases" and e["actor"] is None for e in events))
        self.assertTrue(all("Bearer" not in (e["detail"] or "") for e in events))

        admin = {"Authorization": f"Bearer {self.admin_token}"}
        path = self.root / "download-event.txt"
        path.write_text("内容", encoding="utf-8")
        with transaction() as conn:
            doc_id = conn.execute(
                "INSERT INTO documents(case_id,name,stored_path,mime_type,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (self.cases[0], "download-event.txt", str(path), "text/plain", now(), now())).lastrowid
        self.assertEqual(self.client.get(f"/api/documents/{doc_id}/file", headers=admin).status_code, 200)
        with closing(connect()) as conn:
            download = conn.execute(
                "SELECT outcome,actor FROM security_events WHERE event_type='document_download' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(download["outcome"], "success")
        self.assertEqual(download["actor"], "管理员")

    def test_security_events_survive_case_deletion(self):
        from app.db import connect
        self.client.get(f"/api/dashboard", headers=self.headers)  # record a denial
        # Business deletion cascades case-owned rows; the security trail has
        # no foreign key and must survive it.
        with transaction() as conn:
            conn.execute("DELETE FROM cases WHERE id=?", (self.cases[1],))
        with closing(connect()) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM security_events WHERE request_path='/api/dashboard'").fetchone()[0]
            cases_left = conn.execute("SELECT COUNT(*) FROM cases WHERE id=?", (self.cases[1],)).fetchone()[0]
        self.assertEqual(cases_left, 0)
        self.assertGreaterEqual(remaining, 1)

    def test_evidence_confirmation_records_approver_and_clears_on_reopen(self):
        admin = {"Authorization": f"Bearer {self.admin_token}"}
        created = self.client.post(f"/api/cases/{self.cases[0]}/evidence", headers=admin,
                                   json={"title": "审批归属证据", "category": "书证", "fact": "待核事实", "status": "待复核"})
        evidence_id = created.json()["evidence"]["id"]
        confirmed = self.client.patch(f"/api/evidence/{evidence_id}", headers=admin, json={"status": "已确认"})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["evidence"]["approved_by"], "管理员")
        self.assertTrue(confirmed.json()["evidence"]["approved_at"])
        reopened = self.client.patch(f"/api/evidence/{evidence_id}", headers=admin, json={"status": "待复核"})
        self.assertIsNone(reopened.json()["evidence"]["approved_by"])
        self.assertIsNone(reopened.json()["evidence"]["approved_at"])

    def test_editing_confirmed_content_voids_the_approval(self):
        admin = {"Authorization": f"Bearer {self.admin_token}"}
        created = self.client.post(f"/api/cases/{self.cases[0]}/evidence", headers=admin,
                                   json={"title": "失效传播证据", "category": "书证", "fact": "原事实", "status": "已确认"})
        evidence_id = created.json()["evidence"]["id"]
        self.assertEqual(created.json()["evidence"]["approved_by"], "管理员")
        # Content edit without re-confirmation: approval is voided explicitly.
        edited = self.client.patch(f"/api/evidence/{evidence_id}", headers=admin, json={"quote": "修订后的引用"})
        self.assertEqual(edited.status_code, 200, edited.text)
        body = edited.json()["evidence"]
        self.assertEqual(body["status"], "待复核")
        self.assertIsNone(body["approved_by"])
        self.assertIsNone(body["approved_at"])
        # Editing together with an explicit re-confirmation is a fresh approval
        # of the new content.
        reconfirmed = self.client.patch(f"/api/evidence/{evidence_id}", headers=admin,
                                        json={"quote": "再确认引用", "status": "已确认"})
        self.assertEqual(reconfirmed.json()["evidence"]["status"], "已确认")
        self.assertEqual(reconfirmed.json()["evidence"]["approved_by"], "管理员")

    def test_permission_matrix_restricts_view_only_and_approval(self):
        viewer_token = "c" * 40
        editor_token = "d" * 40
        env = patch.dict(os.environ, {"LAW_REVIEW_API_TOKENS_JSON": json.dumps({
            self.token: {"name": "律师甲", "case_ids": [self.cases[0]]},
            self.admin_token: {"name": "管理员", "admin": True},
            viewer_token: {"name": "只读律师", "case_ids": [self.cases[0]], "permissions": ["view"]},
            editor_token: {"name": "录入律师", "case_ids": [self.cases[0]], "permissions": ["view", "edit"]},
        })})
        env.start()
        self.addCleanup(env.stop)
        viewer = {"Authorization": f"Bearer {viewer_token}"}
        editor = {"Authorization": f"Bearer {editor_token}"}
        # View-only: reads work, any mutation/export/creation is denied.
        self.assertEqual(self.client.get(f"/api/cases/{self.cases[0]}", headers=viewer).status_code, 200)
        self.assertEqual(self.client.post(f"/api/cases/{self.cases[0]}/evidence", headers=viewer,
                                          json={"title": "只读测试", "category": "书证", "fact": "事实"}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/cases/{self.cases[0]}/export", headers=viewer).status_code, 403)
        self.assertEqual(self.client.post("/api/cases", headers=viewer, json={"title": "越权"}).status_code, 403)
        # Edit without approve: metadata updates pass, confirmation is denied.
        created = self.client.post(f"/api/cases/{self.cases[0]}/evidence", headers=editor,
                                   json={"title": "录入测试", "category": "书证", "fact": "事实"})
        self.assertEqual(created.status_code, 201, created.text)
        evidence_id = created.json()["evidence"]["id"]
        self.assertEqual(self.client.patch(f"/api/evidence/{evidence_id}", headers=editor,
                                           json={"credibility": "中"}).status_code, 200)
        denied = self.client.patch(f"/api/evidence/{evidence_id}", headers=editor, json={"status": "已确认"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "需要审批确认权限")
        # Admin approval still works and is attributed.
        confirmed = self.client.patch(f"/api/evidence/{evidence_id}", headers={"Authorization": f"Bearer {self.admin_token}"},
                                      json={"status": "已确认"})
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["evidence"]["approved_by"], "管理员")

    def test_invalid_permission_values_fail_closed(self):
        for value in ('{"permissions": ["root"]}', '{"permissions": []}', '{"permissions": "view"}'):
            payload = dict(self._token_config())
            payload[self.token]["permissions"] = json.loads(value)["permissions"]
            with self.subTest(value=value), patch.dict(os.environ, {"LAW_REVIEW_API_TOKENS_JSON": json.dumps(payload)}):
                self.assertEqual(self.client.get("/api/auth/me").status_code, 503)

    def _token_config(self):
        return {
            self.token: {"name": "律师甲", "case_ids": [self.cases[0]]},
            self.admin_token: {"name": "管理员", "admin": True},
        }

    def test_legacy_principal_keeps_prior_abilities_and_me_lists_permissions(self):
        # No "permissions" key configured: prior behaviour (view/edit/approve/export
        # inside scoped cases, no global management) must be preserved exactly.
        me = self.client.get("/api/auth/me", headers=self.headers)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(sorted(me.json()["permissions"]), ["approve", "edit", "export", "view"])
        created = self.client.post(f"/api/cases/{self.cases[0]}/evidence", headers=self.headers,
                                   json={"title": "遗留权限证据", "category": "书证", "fact": "事实", "status": "已确认"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["evidence"]["approved_by"], "律师甲")
        self.assertEqual(self.client.post("/api/cases", headers=self.headers, json={"title": "x"}).status_code, 403)

    def test_oidc_unconfigured_is_disabled_and_partial_config_fails_closed(self):
        self.assertEqual(self.client.get("/api/auth/oidc/login").status_code, 404)
        for partial in ({"LAW_REVIEW_OIDC_ISSUER": "https://idp.example"},
                        {"LAW_REVIEW_OIDC_CLIENT_ID": "app"}):
            with self.subTest(partial=partial), patch.dict(os.environ, partial):
                self.assertEqual(self.client.get("/api/auth/oidc/login").status_code, 503)
        with patch.dict(os.environ, {"LAW_REVIEW_OIDC_ISSUER": "http://idp.example",
                                     "LAW_REVIEW_OIDC_CLIENT_ID": "app",
                                     "LAW_REVIEW_OIDC_CLIENT_SECRET": "s"}):
            self.assertEqual(self.client.get("/api/auth/oidc/login").status_code, 503)

    def test_oidc_login_flow_with_mock_provider(self):
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = private_key.public_key().public_numbers()
        def int_to_b64(value):
            import base64
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        jwks = {"keys": [{"kty": "RSA", "kid": "test-key", "alg": "RS256", "use": "sig",
                          "n": int_to_b64(public_pem.n), "e": int_to_b64(public_pem.e)}]}
        discovery = {"authorization_endpoint": "https://idp.example/authorize",
                     "token_endpoint": "https://idp.example/token", "jwks_uri": "https://idp.example/jwks"}
        state = nonce = None
        captured_authorize = {}

        def fake_fetch(url, *, data=None, timeout=5):
            if url.endswith("openid-configuration"):
                return discovery
            if url.endswith("/jwks"):
                return jwks
            if url.endswith("/authorize"):
                captured_authorize.update({"url": url, "query": url.split("?", 1)[1]})
                return {}
            if url.endswith("/token"):
                self.assertEqual(data["grant_type"], "authorization_code")
                code = data["code"]
                flows = security_module._oidc_flows
                claims = {"iss": "https://idp.example", "aud": "lexvault-client", "nonce": flows[code][0] if code in flows else "stale",
                          "sub": "user-001", "email": "lv@example.com", "exp": 9999999999}
                token = pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
                return {"id_token": token, "access_token": "at"}
            raise AssertionError(url)

        security_module = __import__("app.security", fromlist=["security"])
        principal_env = patch.dict(os.environ, {
            "LAW_REVIEW_OIDC_ISSUER": "https://idp.example", "LAW_REVIEW_OIDC_CLIENT_ID": "lexvault-client",
            "LAW_REVIEW_OIDC_CLIENT_SECRET": "secret",
            "LAW_REVIEW_OIDC_PRINCIPALS_JSON": json.dumps({"user-001": {"name": "外聘律师", "case_ids": [self.cases[0]], "permissions": ["view", "edit"]}}),
        })
        principal_env.start()
        self.addCleanup(principal_env.stop)
        with patch.object(security_module, "fetch_json", side_effect=fake_fetch):
            from urllib.parse import parse_qs
            import urllib.parse as parse
            # discover endpoints through login redirect
            login_response = self.client.get("/api/auth/oidc/login", follow_redirects=False)
            self.assertEqual(login_response.status_code, 302, login_response.text)
            authorize_url = login_response.headers["location"]
            query = parse_qs(parse.urlparse(authorize_url).query)
            self.assertEqual(query["client_id"], ["lexvault-client"])
            self.assertIn("openid", query["scope"][0])
            state, nonce = query["state"][0], query["nonce"][0]
            # register a fake authorization code bound to that flow nonce
            security_module._oidc_flows["auth-code-1"] = (nonce, security_module.time.monotonic() + 60)
            with patch.object(security_module, "fetch_json", side_effect=fake_fetch):
                callback = self.client.get(f"/api/auth/oidc/callback?code=auth-code-1&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 302, callback.text)
        self.assertEqual(callback.headers["location"], "/")
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.json()["name"], "外聘律师")
        self.assertEqual(sorted(me.json()["permissions"]), ["edit", "view"])
        # The mapped principal's case scope is enforced.
        self.assertEqual(self.client.get(f"/api/cases/{self.cases[0]}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/cases/{self.cases[1]}").status_code, 404)
        # State is single use: replay is rejected.
        replay = self.client.get("/api/auth/oidc/callback?code=auth-code-2&state=" + state, follow_redirects=False)
        self.assertEqual(replay.status_code, 400)

    def test_oidc_unmapped_identity_and_bad_audience_rejected(self):
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        security_module = __import__("app.security", fromlist=["security"])
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_numbers = private_key.public_key().public_numbers()
        import base64
        def int_to_b64(value):
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        jwks = {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                          "n": int_to_b64(public_numbers.n), "e": int_to_b64(public_numbers.e)}]}
        discovery = {"authorization_endpoint": "https://idp.example/authorize",
                     "token_endpoint": "https://idp.example/token", "jwks_uri": "https://idp.example/jwks"}
        env = patch.dict(os.environ, {
            "LAW_REVIEW_OIDC_ISSUER": "https://idp.example", "LAW_REVIEW_OIDC_CLIENT_ID": "lexvault-client",
            "LAW_REVIEW_OIDC_CLIENT_SECRET": "secret",
            "LAW_REVIEW_OIDC_PRINCIPALS_JSON": json.dumps({"known-user": {"name": "已授权"}}),
        })
        env.start()
        self.addCleanup(env.stop)

        def token_for(claims):
            return pyjwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "k1"})

        base_claims = {"iss": "https://idp.example", "aud": "lexvault-client", "sub": "stranger", "exp": 9999999999, "nonce": "nonce-x"}
        def fake_fetch(url, *, data=None, timeout=5):
            if url.endswith("openid-configuration"):
                return discovery
            if url.endswith("/jwks"):
                return jwks
            if url.endswith("/token"):
                return {"id_token": token_for(base_claims)}
            raise AssertionError(url)
        with patch.object(security_module, "fetch_json", side_effect=lambda url, **kw: discovery if url.endswith("openid-configuration") else {}):
            self.client.get("/api/auth/oidc/login", follow_redirects=False)
        with patch.object(security_module, "fetch_json", side_effect=fake_fetch):
            with transaction() as conn:
                state = "manual-state"
                conn.execute("SELECT 1")
            security_module._oidc_flows["flow-stranger"] = ("nonce-x", security_module.time.monotonic() + 60)
            stranger = self.client.get("/api/auth/oidc/callback?code=c&state=flow-stranger", follow_redirects=False)
        self.assertEqual(stranger.status_code, 403)
        self.assertNotIn("lexvault_session", stranger.headers.get("set-cookie", ""))
        # Wrong audience fails validation with a sanitized message.
        security_module._oidc_flows["flow-badaud"] = ("nonce-y", security_module.time.monotonic() + 60)
        bad_claims = dict(base_claims, sub="known-user", aud="someone-else", nonce="nonce-y")
        def bad_fetch(url, *, data=None, timeout=5):
            if url.endswith("openid-configuration"):
                return discovery
            if url.endswith("/jwks"):
                return jwks
            return {"id_token": token_for(bad_claims)}
        with patch.object(security_module, "fetch_json", side_effect=bad_fetch):
            bad = self.client.get("/api/auth/oidc/callback?code=c&state=flow-badaud", follow_redirects=False)
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn("lexvault-client", bad.text)
        with closing(connect()) as conn:
            failures = conn.execute("SELECT COUNT(*) FROM security_events WHERE event_type='auth' AND outcome='login_failed' AND detail LIKE '%OIDC%'").fetchone()[0]
        self.assertGreaterEqual(failures, 2)

    def test_export_template_crud_requires_manage_and_custom_templates_render(self):
        viewer = self.headers
        admin = {"Authorization": f"Bearer {self.admin_token}"}
        listed = self.client.get("/api/export-templates", headers=viewer)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertGreaterEqual(len(listed.json()), 2)
        self.assertEqual(self.client.get("/api/export-templates/1", headers=viewer).status_code, 403)
        body = {
            "name": "测试质证模板",
            "description": "只导出已确认证据",
            "blocks": [
                {"type": "evidence_table", "title": "已确认证据", "filename": "confirmed.csv", "evidence_status": "已确认", "required": True},
                {"type": "static_markdown", "title": "律师说明", "filename": "说明.md", "content": "律师复核说明", "required": True},
            ],
        }
        self.assertEqual(self.client.post("/api/export-templates", headers=viewer, json=body).status_code, 403)
        created = self.client.post("/api/export-templates", headers=admin, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        template_id = created.json()["id"]
        detail = self.client.get(f"/api/export-templates/{template_id}", headers=admin)
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()["builtin"])
        self.assertEqual(self.client.delete("/api/export-templates/1", headers=admin).status_code, 409)
        rejected = self.client.get(f"/api/cases/{self.cases[0]}/export?template_id={template_id}&final=true", headers=admin)
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("缺少必选内容", rejected.text)
        preview = self.client.get(f"/api/cases/{self.cases[0]}/export?template_id={template_id}", headers=admin)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.headers.get("x-package-sha256"))

        # Add the required source/evidence and verify a real final package.
        source = self.root / "final-source.txt"
        source.write_text("结案原始材料", encoding="utf-8")
        with transaction() as conn:
            document_id = conn.execute(
                "INSERT INTO documents(case_id,name,stored_path,mime_type,pages,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (self.cases[0], "final-source.txt", str(source), "text/plain", 1, "已索引", now(), now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO evidence(case_id,title,category,fact,credibility,status,source_document_id,source_page_start,source_page_end,quote,approved_by,approved_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.cases[0], "已确认结案事实", "书证", "材料支持该事实", "高", "已确认", document_id, 1, 1, "结案原始材料", "管理员", now(), now()),
            )
        final = self.client.get(f"/api/cases/{self.cases[0]}/export?template_id={template_id}&final=true", headers=admin)
        self.assertEqual(final.status_code, 200, final.text)
        self.assertTrue(final.headers.get("x-package-sha256"))

    def test_export_template_invalid_blocks_fail_closed(self):
        admin = {"Authorization": f"Bearer {self.admin_token}"}
        invalid = {"name": "错误模板", "blocks": [{"type": "static_markdown", "title": "空", "content": ""}]}
        response = self.client.post("/api/export-templates", headers=admin, json=invalid)
        self.assertEqual(response.status_code, 400)
        self.assertIn("必须填写内容", response.text)

    def test_export_template_update_and_delete_custom(self):
        admin = {"Authorization": f"Bearer {self.admin_token}"}
        body = {"name": "可删除模板", "description": "", "blocks": [{"type": "case_summary", "title": "摘要", "required": False}]}
        created = self.client.post("/api/export-templates", headers=admin, json=body)
        self.assertEqual(created.status_code, 201)
        template_id = created.json()["id"]
        updated = self.client.put(f"/api/export-templates/{template_id}", headers=admin,
                                  json={**body, "name": "已更新模板"})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(self.client.delete(f"/api/export-templates/{template_id}", headers=admin).status_code, 200)
        self.assertEqual(self.client.get(f"/api/export-templates/{template_id}", headers=admin).status_code, 404)

    def test_cross_site_get_cannot_generate_export(self):
        headers = {"Authorization": f"Bearer {self.admin_token}", "Sec-Fetch-Site": "cross-site", "Origin": "https://attacker.example"}
        export_dir = self.root / "exports"
        before = set(export_dir.glob("*.zip"))
        self.assertEqual(self.client.get(f"/api/cases/{self.cases[0]}/export", headers=headers).status_code, 403)
        self.assertEqual(set(export_dir.glob("*.zip")), before)

    def test_bad_configuration_fails_closed(self):
        for value in ("{}", '{"short":{"name":"甲"}}', "[]", "not json"):
            with self.subTest(value=value), patch.dict(os.environ, {"LAW_REVIEW_API_TOKENS_JSON": value}):
                self.assertEqual(self.client.get("/api/auth/me").status_code, 503)
                with self.assertRaises(ValueError):
                    configured_tokens()

    def test_local_default_rejects_remote_peer(self):
        with patch.dict(os.environ, {"LAW_REVIEW_AUTH_MODE": "local"}):
            remote = TestClient(app, client=("198.51.100.2", 5000))
            try:
                self.assertEqual(remote.get("/api/auth/me").status_code, 403)
            finally:
                remote.close()
