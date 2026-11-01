"""Static UI delivery cannot bypass authentication or expose runtime files."""
import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import db_scope, init_db
from app.main import app


class FrontendDeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-delivery-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ui = self.root / "ui"
        (self.ui / "_next/static").mkdir(parents=True)
        self.script = 'self.__next_f = [["合成 UI"]];'
        self.html = f'<html><head><script src="/_next/static/ui.js"></script></head><body><script>{self.script}</script></body></html>'
        (self.ui / "index.html").write_text(self.html)
        (self.ui / "_next/static/ui.js").write_text("console.log('synthetic');")
        (self.root / "private.js").write_text("private fixture")
        scope = db_scope(self.root / "data.sqlite")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        env = patch.dict(os.environ, {
            "LAW_REVIEW_FRONTEND_DIR": str(self.ui), "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({"t" * 40: {"name": "测试", "admin": True}}),
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_desktop_and_mobile_share_one_shell_with_exact_inline_script_hash(self):
        digest = base64.b64encode(hashlib.sha256(self.script.encode()).digest()).decode()
        for path in ("/", "/mobile?auth_error=expired"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, self.html)
                csp = response.headers["content-security-policy"]
                script_policy = csp.split("script-src ", 1)[1].split(";", 1)[0]
                self.assertIn(f"'sha256-{digest}'", script_policy)
                self.assertNotIn("'unsafe-inline'", script_policy)
                self.assertIn("object-src 'none'", csp)
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_hashed_assets_are_public_but_api_stays_private(self):
        response = self.client.get("/_next/static/ui.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("immutable", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/api/cases").status_code, 401)
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/session", json={"token": "t" * 40}, headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_assets_reject_traversal_sources_secrets_and_symlink_escape(self):
        for path in ("/_next/%2e%2e/%2e%2e/private.js", "/_next/static/missing.js", "/_next/static/.env"):
            self.assertEqual(self.client.get(path).status_code, 404)
        (self.ui / "_next/static/leak.js").symlink_to(self.root / "private.js")
        self.assertEqual(self.client.get("/_next/static/leak.js").status_code, 404)
        (self.ui / "_next/static/config.json").write_text('{"fixture":"private"}')
        self.assertEqual(self.client.get("/_next/static/config.json").status_code, 404)

    def test_next_root_symlink_cannot_escape_the_public_build(self):
        other = self.root / "other-ui"
        other.mkdir()
        (other / "index.html").write_text(self.html)
        (other / "_next").symlink_to(self.root)
        with patch.dict(os.environ, {"LAW_REVIEW_FRONTEND_DIR": str(other)}):
            self.assertEqual(self.client.get("/_next/private.js").status_code, 404)

    def test_missing_build_and_symlink_shell_fail_closed(self):
        (self.ui / "index.html").unlink()
        self.assertEqual(self.client.get("/").status_code, 503)
        (self.ui / "index.html").symlink_to(self.root / "private.js")
        self.assertEqual(self.client.get("/").status_code, 503)

    def test_local_mode_still_rejects_forwarded_proxy_identity(self):
        with patch.dict(os.environ, {"LAW_REVIEW_AUTH_MODE": "local"}):
            response = self.client.get("/api/auth/me")
            self.assertTrue(response.json()["authenticated"])
            self.assertEqual(self.client.get("/api/cases", headers={"X-Forwarded-Host": "localhost"}).status_code, 403)
