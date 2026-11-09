"""Synthetic offline backup drills; never open the application's real data directory."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.ops.data_snapshot import backup, digest, restore, verify


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lexvault-snapshot-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source-data"
        self.source.mkdir()
        self.document = self.source / "uploads/1/fixture.txt"
        self.document.parent.mkdir(parents=True)
        self.document.write_text("synthetic evidence fixture", encoding="utf-8")
        (self.source / "exports").mkdir()
        (self.source / "exports/fixture.zip").write_bytes(b"synthetic export")
        # Approved report bodies live outside uploads/exports; a restore must
        # carry them or a delivered version would lose its frozen text.
        self.artifact = self.source / "report_versions/case-1/report-1-1-deadbeef/001-案件摘要.md"
        self.artifact.parent.mkdir(parents=True)
        self.artifact.write_text("synthetic frozen body", encoding="utf-8")
        with closing(sqlite3.connect(self.source / "law_review.db")) as database, database:
            database.executescript("""
                CREATE TABLE documents(id INTEGER PRIMARY KEY, stored_path TEXT);
                CREATE TABLE batch_import_files(id INTEGER PRIMARY KEY, stored_path TEXT, status TEXT);
                CREATE TABLE agent_runs(id INTEGER PRIMARY KEY, status TEXT, checkpoint_thread_id TEXT);
                CREATE TABLE batch_imports(id INTEGER PRIMARY KEY, status TEXT);
                CREATE TABLE review_jobs(id INTEGER PRIMARY KEY, status TEXT);
            """)
            database.execute("INSERT INTO documents VALUES(1, ?)", (str(self.document),))
            database.execute("INSERT INTO batch_import_files VALUES(1, ?, 'completed')",
                             (str(self.source / "uploads/batch_temp/1/deleted.txt"),))
            database.execute("INSERT INTO agent_runs VALUES(1, 'failed', 'agent-run-1')")
        with closing(sqlite3.connect(self.source / "langgraph_checkpoints.sqlite")) as database, database:
            database.execute("CREATE TABLE checkpoints(thread_id TEXT, value TEXT)")
            database.execute("INSERT INTO checkpoints VALUES('agent-run-1','synthetic persisted state')")
        self.snapshot = self.root / "backup"

    def test_backup_restore_after_originals_are_gone(self) -> None:
        original_hash = digest(self.source / "law_review.db")
        backup(self.source, self.snapshot, quiescent=True)
        self.assertEqual(digest(self.source / "law_review.db"), original_hash)
        # Only remove this test-owned temporary fixture, simulating disaster recovery.
        shutil.rmtree(self.source)
        target = self.root / "restored"
        result = restore(self.snapshot, target)
        self.assertEqual(result["relocated_paths"], 2)
        self.assertEqual((target / "uploads/1/fixture.txt").read_text(), "synthetic evidence fixture")
        self.assertTrue((target / "exports/fixture.zip").is_file())
        self.assertEqual(
            (target / "report_versions/case-1/report-1-1-deadbeef/001-案件摘要.md").read_text(),
            "synthetic frozen body",
        )
        with closing(sqlite3.connect(target / "law_review.db")) as database:
            self.assertEqual(database.execute("SELECT stored_path FROM documents").fetchone()[0], str(target / "uploads/1/fixture.txt"))
            self.assertEqual(database.execute("SELECT stored_path FROM batch_import_files").fetchone()[0], str(target / "uploads/batch_temp/1/deleted.txt"))
        with closing(sqlite3.connect(target / "langgraph_checkpoints.sqlite")) as database:
            self.assertEqual(database.execute("SELECT value FROM checkpoints").fetchone()[0], "synthetic persisted state")
        verify(self.snapshot)

    def test_requires_quiescent_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "quiescent"):
            backup(self.source, self.snapshot)
        self.assertFalse(self.snapshot.exists())

    def test_active_jobs_are_rejected(self) -> None:
        for table in ("agent_runs", "batch_import_files", "batch_imports", "review_jobs"):
            with self.subTest(table=table):
                with closing(sqlite3.connect(self.source / "law_review.db")) as database, database:
                    if table in {"batch_imports", "review_jobs"}:
                        database.execute(f"INSERT INTO {table} VALUES(1,'queued')")
                    else:
                        database.execute(f"UPDATE {table} SET status=?", ("running" if table == "agent_runs" else "pending",))
                with self.assertRaisesRegex(ValueError, "Active work"):
                    backup(self.source, self.snapshot, quiescent=True)
                with closing(sqlite3.connect(self.source / "law_review.db")) as database, database:
                    database.execute(f"UPDATE {table} SET status='completed'")

    def test_existing_target_is_not_overwritten(self) -> None:
        backup(self.source, self.snapshot, quiescent=True)
        with self.assertRaisesRegex(ValueError, "must not exist"):
            backup(self.source, self.snapshot, quiescent=True)
        with self.assertRaisesRegex(ValueError, "must not exist"):
            restore(self.snapshot, self.source)

    def test_tampering_is_rejected_before_restore_directory_is_created(self) -> None:
        backup(self.source, self.snapshot, quiescent=True)
        (self.snapshot / "uploads/1/fixture.txt").write_text("tampered fixture", encoding="utf-8")
        target = self.root / "restored"
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore(self.snapshot, target)
        self.assertFalse(target.exists())

    def test_symlink_cannot_escape_source(self) -> None:
        (self.source / "uploads/link").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            backup(self.source, self.snapshot, quiescent=True)

    def test_snapshot_path_traversal_is_rejected(self) -> None:
        backup(self.source, self.snapshot, quiescent=True)
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"][0]["path"] = "../escape"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsafe manifest"):
            verify(self.snapshot)

    def test_outside_document_path_is_rejected(self) -> None:
        with closing(sqlite3.connect(self.source / "law_review.db")) as database, database:
            database.execute("UPDATE documents SET stored_path='/private/outside.txt'")
        with self.assertRaisesRegex(ValueError, "outside the data"):
            backup(self.source, self.snapshot, quiescent=True)

    def test_changes_during_backup_leave_incomplete_target(self) -> None:
        original_copy = shutil.copytree

        def change_source(*args: Any, **kwargs: Any) -> object:
            result = original_copy(*args, **kwargs)
            self.document.write_text("changed during copy", encoding="utf-8")
            return result

        with patch("scripts.ops.data_snapshot.shutil.copytree", side_effect=change_source):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                backup(self.source, self.snapshot, quiescent=True)
        self.assertTrue((self.snapshot / "INCOMPLETE").exists())
        with self.assertRaisesRegex(ValueError, "Incomplete snapshot"):
            verify(self.snapshot)

    def test_custom_checkpoint_file_is_preserved(self) -> None:
        external = self.root / "custom-checkpoints.sqlite"
        (self.source / "langgraph_checkpoints.sqlite").rename(external)
        with self.assertRaisesRegex(ValueError, "missing checkpoint"):
            backup(self.source, self.snapshot, quiescent=True)
        backup(self.source, self.snapshot, checkpoint=external, quiescent=True)
        self.assertTrue((self.snapshot / "langgraph_checkpoints.sqlite").is_file())

    def test_model_override_is_restored_without_copying_other_settings(self) -> None:
        from app.core.config import LLMConfig
        from app.storage.db import db_scope

        settings = self.source / "model_settings.json"
        settings.write_text(json.dumps({"base_url": "http://127.0.0.1:12345/v1", "model": "synthetic-validated-model"}))
        settings.chmod(0o600)
        (self.source / ".env").write_text("SYNTHETIC_SECRET=never-backup")
        manifest = backup(self.source, self.snapshot, quiescent=True)
        target = self.root / "restored"
        report = restore(self.snapshot, target)
        self.assertTrue(manifest["model_settings_included"])
        self.assertEqual(report["model_settings"], "restored")
        self.assertEqual((target / "model_settings.json").read_bytes(), settings.read_bytes())
        self.assertEqual((target / "model_settings.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((target / ".env").exists())
        with db_scope(target / "law_review.db"):
            restored = LLMConfig.load()
        self.assertEqual((restored.base_url, restored.model), ("http://127.0.0.1:12345/v1", "synthetic-validated-model"))

    def test_invalid_or_secret_model_settings_are_rejected(self) -> None:
        invalid = ["broken JSON", "{}", "[]", '{"model":"first","model":"second","base_url":"http://localhost/v1"}',
                   json.dumps({"base_url": "http://localhost/v1", "model": ""}),
                   json.dumps({"base_url": "http://localhost/v1", "model": "model", "api_key": "synthetic"}),
                   json.dumps({"base_url": "http://synthetic:secret@localhost/v1", "model": "model"}),
                   json.dumps({"base_url": "http://localhost/v1?api_key=synthetic", "model": "model"})]
        for payload in invalid:
            with self.subTest(payload=payload):
                (self.source / "model_settings.json").write_text(payload)
                with self.assertRaisesRegex(ValueError, "model_settings"):
                    backup(self.source, self.snapshot, quiescent=True)
                self.assertFalse(self.snapshot.exists())

    def test_model_settings_symlink_is_rejected(self) -> None:
        external = self.root / "external-model-settings.json"
        external.write_text('{"base_url":"http://localhost/v1","model":"synthetic"}')
        (self.source / "model_settings.json").symlink_to(external)
        with self.assertRaisesRegex(ValueError, "model_settings"):
            backup(self.source, self.snapshot, quiescent=True)
        self.assertFalse(self.snapshot.exists())

    def test_legacy_snapshot_restores_and_reports_model_settings_absent(self) -> None:
        backup(self.source, self.snapshot, quiescent=True)
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("model_settings_included")
        manifest_path.write_text(json.dumps(manifest))
        verify(self.snapshot)
        report = restore(self.snapshot, self.root / "restored")
        self.assertEqual(report["model_settings"], "absent_verify_deployment_defaults")

    def test_invalid_model_settings_in_snapshot_rejected_before_restore(self) -> None:
        (self.source / "model_settings.json").write_text('{"base_url":"http://localhost/v1","model":"synthetic"}')
        backup(self.source, self.snapshot, quiescent=True)
        settings = self.snapshot / "model_settings.json"
        settings.write_text("{}")
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        entry = next(item for item in manifest["files"] if item["path"] == "model_settings.json")
        entry.update(size=settings.stat().st_size, sha256=digest(settings))
        manifest_path.write_text(json.dumps(manifest))
        target = self.root / "restored"
        with self.assertRaisesRegex(ValueError, "model_settings"):
            restore(self.snapshot, target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
