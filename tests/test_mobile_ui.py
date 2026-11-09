"""Mobile entry, shared helpers and desktop race regressions without a browser."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.storage.db import db_scope, init_db

ROOT = Path(__file__).resolve().parents[1]


class MobileRouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-mobile-")
        self.addCleanup(directory.cleanup)
        scope = db_scope(Path(directory.name) / "test.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        public = Path(directory.name) / "ui"
        (public / "_next/static").mkdir(parents=True)
        (public / "index.html").write_text('<html><script src="/_next/static/ui-a1b2.js"></script><body>合成统一前端</body></html>')
        (public / "_next/static/ui-a1b2.js").write_text("console.log('synthetic shell');")
        env = patch.dict(os.environ, {
            "LAW_REVIEW_FRONTEND_DIR": str(public),
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({"m" * 40: {"name": "测试", "admin": True}}),
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_public_shells_and_assets_have_same_security_policy(self):
        desktop = self.client.get("/")
        for path in ("/", "/mobile"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertEqual(response.headers["content-security-policy"], desktop.headers["content-security-policy"])
                self.assertEqual(response.headers["x-frame-options"], "DENY")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        mobile = self.client.get("/mobile?auth_error=expired")
        self.assertIn("text/html", mobile.headers["content-type"])
        self.assertEqual(mobile.text, desktop.text)
        self.assertIn('/_next/static/ui-a1b2.js', desktop.text)
        self.assertNotIn('/assets/', desktop.text)
        asset = self.client.get('/_next/static/ui-a1b2.js')
        self.assertEqual(asset.status_code, 200)
        self.assertIn('immutable', asset.headers['cache-control'])
        self.assertEqual(asset.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(asset.headers['x-frame-options'], 'DENY')
        for retired in ('entry.js', 'shared.js', 'app.js', 'mobile.js', 'mobile.css'):
            self.assertEqual(self.client.get('/assets/' + retired).status_code, 404)

    def test_mobile_shell_does_not_bypass_api_or_host_security(self):
        self.assertEqual(self.client.get("/mobile").status_code, 200)
        denied = self.client.get("/api/cases")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/mobile", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/session", json={"token": "m" * 40}, headers={"Origin": "https://evil.example"}).status_code, 403)
