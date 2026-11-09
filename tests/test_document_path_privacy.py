"""The server-side storage path of an uploaded document never leaves the API.

``documents.stored_path`` is an absolute path inside the data directory. The
client downloads originals through ``/api/documents/{id}/file``; returning the
path would only disclose the server's layout. The download, report delivery and
batch import keep reading it internally.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support import IsolatedDatabaseTestCase

CONTENT = "甲乙双方于2026年1月1日签订借款合同。"


class DocumentPathPrivacyTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.app = app
        cls.client = TestClient(app)
        cls.data_dir = str(Path(os.environ["LAW_REVIEW_DATA_DIR"]).resolve())
        case = cls.client.post("/api/cases", json={"title": "路径隐私案件"})
        assert case.status_code == 201, case.text
        cls.case_id = case.json()["id"]
        cls.upload = cls.client.post(f"/api/cases/{cls.case_id}/documents",
                                     files=[("files", ("借款合同.txt", CONTENT, "text/plain"))])
        assert cls.upload.status_code == 201, cls.upload.text
        cls.document_id = cls.upload.json()["documents"][0]["id"]

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def assert_private(self, response):
        self.assertNotIn("stored_path", response.text)
        self.assertNotIn(self.data_dir, response.text)

    def stored_path(self) -> Path:
        from app.storage.db import connect

        with closing(connect()) as conn:
            return Path(conn.execute("SELECT stored_path FROM documents WHERE id=?", (self.document_id,)).fetchone()[0])

    def test_the_original_is_stored_inside_the_data_directory(self):
        # The guard below is only meaningful if the row really holds a path.
        stored = self.stored_path()
        self.assertTrue(stored.is_file())
        self.assertTrue(stored.resolve().is_relative_to(self.data_dir))

    def test_upload_listing_and_directory_responses_omit_the_path(self):
        self.assert_private(self.upload)
        listing = self.client.get(f"/api/cases/{self.case_id}/documents")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([item["id"] for item in listing.json()], [self.document_id])
        self.assert_private(listing)

        unchanged = self.client.patch(f"/api/documents/{self.document_id}/directory", json={})
        changed = self.client.patch(f"/api/documents/{self.document_id}/directory", json={"summary": "借款十万元"})
        for response in (unchanged, changed):
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_private(response)
        self.assertEqual(changed.json()["summary"], "借款十万元")
        self.assertIn("page_items", changed.json())

    def test_download_still_serves_the_original_bytes(self):
        response = self.client.get(f"/api/documents/{self.document_id}/file")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.stored_path().read_bytes())
        self.assertEqual(response.content.decode(), CONTENT)

    def test_no_read_endpoint_or_export_member_contains_the_path(self):
        ids = {"case_id": self.case_id, "document_id": self.document_id, "page_no": 1}
        swept = 0
        for path in self.app.openapi()["paths"]:
            operations = self.app.openapi()["paths"][path]
            names = re.findall(r"{(\w+)}", path)
            if "get" not in operations or not path.startswith("/api") or not set(names) <= set(ids):
                continue
            url = re.sub(r"{(\w+)}", lambda match: str(ids[match.group(1)]), path)
            response = self.client.get(url, params={"q": "借款"})
            with self.subTest(path=path):
                if response.headers.get("content-type", "").startswith("application/zip"):
                    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                        for member in archive.namelist():
                            text = archive.read(member).decode("utf-8", "replace")
                            self.assertNotIn(self.data_dir, text, member)
                            self.assertNotIn("stored_path", text, member)
                else:
                    self.assert_private(response)
            swept += 1
        # Sanity: the sweep covered the document, page, search and export endpoints.
        self.assertGreater(swept, 15)
