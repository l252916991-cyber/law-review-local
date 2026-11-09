"""Capacity driver safety and digest checks use synthetic local fixtures only."""

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts.acceptance.local_capacity_acceptance import Profile, evaluate, percentile, run, seed_data, verify_package


class CapacityDriverTest(unittest.TestCase):
    def test_fixed_gates_fail_for_latency_errors_memory_disk_and_integrity(self):
        from copy import deepcopy

        baseline = {
            "reads": {"p95_seconds": 1.5, "unexpected_failure_rate": 0},
            "conflicts": {"unexpected_failure_rate": 0},
            "exports": {"p95_seconds": 10.0, "unexpected_failure_rate": 0},
            "signing_steps_seconds": {"submit": 15.0, "approve": 1.0},
            "isolation_passed": True,
            "concurrent_readers_during_export_passed": True,
            "api_peak_rss_bytes": 2 * 1024**3,
            "output_bytes": 2 * 1024**3,
            "disk_free_after_bytes": 2 * 1024**3,
            "database": {"integrity": True, "foreign_keys": True, "formal_delivery_events": 3},
            "data_counts_match": True,
        }
        self.assertTrue(all(evaluate(baseline).values()))
        for field, changes in (
            ("reads", {"p95_seconds": 1.501}),
            ("reads", {"unexpected_failure_rate": 0.002}),
            ("exports", {"p95_seconds": 10.001}),
            ("database", {"foreign_keys": False}),
        ):
            with self.subTest(field=field, changes=changes):
                observed = deepcopy(baseline)
                observed[field].update(changes)
                self.assertFalse(all(evaluate(observed).values()))
        for field, value in (
            ("api_peak_rss_bytes", 2 * 1024**3 + 1),
            ("api_peak_rss_bytes", 0),
            ("output_bytes", 2 * 1024**3 + 1),
            ("disk_free_after_bytes", 2 * 1024**3 - 1),
        ):
            with self.subTest(field=field):
                observed = deepcopy(baseline)
                observed[field] = value
                self.assertFalse(all(evaluate(observed).values()))

    def test_existing_output_is_never_opened_or_overwritten(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)
            marker = path / "law_review.db"
            marker.write_bytes(b"synthetic existing database")
            with patch("scripts.acceptance.local_capacity_acceptance.seed_data") as seed:
                with self.assertRaisesRegex(ValueError, "new directory"):
                    run(path)
                seed.assert_not_called()
            self.assertEqual(marker.read_bytes(), b"synthetic existing database")
            self.assertEqual(list(path.iterdir()), [marker])

    def test_seed_has_exact_pages_transactions_and_refuses_reuse(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "data"
            profile = Profile(documents=2, pages_per_document=3, evidence=4, transactions=7)
            ids = seed_data(path, profile)
            with closing(sqlite3.connect((path / "law_review.db").as_uri() + "?mode=ro", uri=True)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 6)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 4)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM bank_transactions").fetchone()[0], 7)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertNotEqual(ids["case_id"], ids["other_case_id"])
            with self.assertRaisesRegex(ValueError, "must be new"):
                seed_data(path, profile)

    def test_percentile_is_nearest_rank_and_rejects_no_observations(self):
        self.assertEqual(percentile(list(range(1, 101)), 0.95), 95)
        self.assertEqual(percentile([3, 1, 2], 0.5), 2)
        with self.assertRaises(ValueError):
            percentile([], 0.95)

    def test_package_checks_preview_full_csv_and_attachment_set(self):
        body_text = b"synthetic frozen body"
        original = b"original"
        header = "date,account,direction,amount,currency,party,memo,row,status\r\n"
        row = "2026-01-01,account,inflow,1200,CNY,party,memo,1,parsed\r\n"
        expected_sheet = (header + row * 7).encode()
        preview = {
            "report": {"id": 3, "content_hash": "synthetic", "frozen_version_id": 9},
            "blocks": [{"archive_name": "body.md", "sha256": hashlib.sha256(body_text).hexdigest()}],
            "attachments": [
                {"archive_name": "evidence.txt", "sha256": hashlib.sha256(original).hexdigest(), "size": len(original)}
            ],
            "transactions": {
                "archive_name": "sheet.csv",
                "count": 7,
                "sha256": hashlib.sha256(expected_sheet).hexdigest(),
            },
        }

        def package(*, tampered=False, actual_rows=7, omit_attachment=False, wrong_body=False):
            sheet = (header + row * actual_rows).encode()
            manifest = {
                "finalized": True,
                "report": {"id": 3, "content_hash": "synthetic", "approved_by": "reviewer"},
                "frozen": {"version_id": 9},
                "files": [] if omit_attachment else preview["attachments"],
                "artifacts": [
                    {"archive_name": "sheet.csv", "transaction_count": 7, "sha256": hashlib.sha256(sheet).hexdigest()}
                ],
            }
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("清单.json", json.dumps(manifest))
                if not omit_attachment:
                    archive.writestr("evidence.txt", b"tampered" if tampered else original)
                archive.writestr("body.md", b"wrong frozen body" if wrong_body else body_text)
                archive.writestr("sheet.csv", sheet)
            return buffer.getvalue()

        self.assertEqual(verify_package(package(), 7, 1, 3, preview, "reviewer")["transactions"], 7)
        for rows, files, report in ((8, 1, 3), (7, 2, 3), (7, 1, 4)):
            with self.subTest(rows=rows, files=files, report=report), self.assertRaises(ValueError):
                verify_package(package(), rows, files, report, preview, "reviewer")
        for options in ({"tampered": True}, {"omit_attachment": True}, {"wrong_body": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                verify_package(package(**options), 7, 1, 3, preview, "reviewer")
        # Even an independently consistent digest cannot make six actual rows
        # satisfy the frozen/declared seven-row count.
        from copy import deepcopy

        short_preview = deepcopy(preview)
        short_preview["transactions"]["sha256"] = hashlib.sha256((header + row * 6).encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "CSV row count"):
            verify_package(package(actual_rows=6), 7, 1, 3, short_preview, "reviewer")
