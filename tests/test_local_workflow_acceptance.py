"""Safety and evidence contract checks for the synthetic five-user HTTP drill."""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.local_workflow_acceptance import Session, check_package, local_url, run, write_json


class LocalWorkflowToolTest(unittest.TestCase):
    def test_target_must_be_a_loopback_origin_without_credentials_or_path(self):
        for origin in ("http://127.0.0.1:8765", "http://localhost:8765/", "http://[::1]:8765"):
            self.assertEqual(local_url(origin), origin.rstrip("/"))
        for origin in (
            "https://example.com",
            "http://127.0.0.1.example.com",
            "file:///tmp/api",
            "http://user:secret@127.0.0.1:8765",
            "http://127.0.0.1/api",
            "http://127.0.0.1?secret=1",
        ):
            with self.subTest(origin=origin), self.assertRaisesRegex(RuntimeError, "loopback"):
                local_url(origin)

    def test_existing_output_is_not_reused_or_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            marker = output / "existing.txt"
            marker.write_text("preserve")
            with patch("scripts.local_workflow_acceptance.exercise") as exercise:
                with self.assertRaisesRegex(RuntimeError, "new directory"):
                    run(output)
                exercise.assert_not_called()
            self.assertEqual(list(output.iterdir()), [marker])
            self.assertEqual(marker.read_text(), "preserve")

    def test_request_journal_contains_no_token_request_or_response_body(self):
        journal = []
        session = Session("http://127.0.0.1:8765", "admin", journal)
        token = "synthetic-private-token"
        with patch("scripts.local_workflow_acceptance._request", return_value=(200, b'{"name":"synthetic"}', {})):
            self.assertEqual(session.login(token), {"name": "synthetic"})
        self.assertNotIn(token, json.dumps(journal))
        self.assertNotIn("synthetic", json.dumps(journal))
        self.assertEqual(journal[0]["status"], 200)
        self.assertEqual(journal[0]["identity"], "admin")

    def test_credential_file_is_private_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            write_json(path, {"token": "synthetic"}, private=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_json(path, {"token": "different"}, private=True)
            self.assertEqual(json.loads(path.read_text()), {"token": "synthetic"})

    def test_package_requires_reviewer_and_complete_frozen_members(self):
        body = b"synthetic body"
        attachment = b"synthetic file"
        sheet = b"date,account,direction,amount,currency,party,memo,row,status\r\n"
        preview = {
            "report": {"id": 7, "content_hash": "synthetic-frozen-hash", "frozen_version_id": 9},
            "blocks": [{"archive_name": "body.md", "sha256": hashlib.sha256(body).hexdigest()}],
            "attachments": [
                {
                    "archive_name": "attachment.txt",
                    "sha256": hashlib.sha256(attachment).hexdigest(),
                    "size": len(attachment),
                }
            ],
            "transactions": {"archive_name": "sheet.csv", "count": 0, "sha256": hashlib.sha256(sheet).hexdigest()},
        }

        def package(*, bad_body=False, bad_file=False, approver="synthetic reviewer", version=9, omit_attachment=False):
            manifest = {
                "finalized": True,
                "report": {"id": 7, "content_hash": "synthetic-frozen-hash", "approved_by": approver},
                "frozen": {"version_id": version},
                "artifacts": [
                    {"archive_name": "sheet.csv", "transaction_count": 0, "sha256": hashlib.sha256(sheet).hexdigest()}
                ],
                "files": [] if omit_attachment else preview["attachments"],
            }
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("清单.json", json.dumps(manifest))
                archive.writestr("body.md", b"changed" if bad_body else body)
                archive.writestr("sheet.csv", sheet)
                if not omit_attachment:
                    archive.writestr("attachment.txt", b"changed" if bad_file else attachment)
            return stream.getvalue()

        checked = check_package(package(), preview, "synthetic reviewer")
        self.assertEqual(set(checked), {"body.md", "attachment.txt", "sheet.csv"})
        for options in (
            {"bad_body": True},
            {"bad_file": True},
            {"approver": "wrong reviewer"},
            {"version": 10},
            {"omit_attachment": True},
        ):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                check_package(package(**options), preview, "synthetic reviewer")


if __name__ == "__main__":
    unittest.main()
