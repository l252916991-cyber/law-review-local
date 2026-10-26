"""Acceptance checker: identity, schema, storage, collaboration and backup drill."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts.collaboration_acceptance import run_checks

ADMIN_TOKEN = "a" * 40


class AcceptanceChecksTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-acceptance-")
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name) / "data"
        self.env = patch.dict(
            os.environ,
            {
                "LAW_REVIEW_DATA_DIR": str(self.data),
                "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(self.data / "checkpoints.sqlite"),
                "LAW_REVIEW_AUTH_MODE": "token",
                "LAW_REVIEW_ALLOWED_HOSTS": "localhost,127.0.0.1",
                "LAW_REVIEW_API_TOKENS_JSON": json.dumps(
                    {ADMIN_TOKEN: {"name": "管理员", "admin": True}}
                ),
                "LAW_REVIEW_ALLOW_INSECURE_LOCAL_TOKEN": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        from app.db import init_db
        init_db(seed=False)

    def checks_by_name(self, report):
        return {check["name"]: check for check in report["checks"]}

    def test_clean_installation_has_no_failures(self):
        report = run_checks(self.data)
        checks = self.checks_by_name(report)
        self.assertEqual(report["summary"]["fail"], 0, report)
        self.assertEqual(checks["auth_mode"]["detail"], "模式：token")
        self.assertEqual(checks["schema_version"]["status"], "pass")
        self.assertEqual(checks["integrity"]["status"], "pass")
        self.assertEqual(checks["administrator"]["status"], "pass")
        self.assertEqual(checks["token_strength"]["status"], "pass")

    def test_short_environment_token_fails(self):
        with patch.dict(os.environ, {"LAW_REVIEW_API_TOKENS_JSON": json.dumps({"short": {"name": "甲", "admin": True}})}):
            report = run_checks(self.data)
        checks = self.checks_by_name(report)
        self.assertEqual(checks["token_strength"]["status"], "fail")
        self.assertEqual(report["summary"]["fail"], 1)

    def test_wildcard_host_and_missing_admin_fail(self):
        with patch.dict(
            os.environ,
            {"LAW_REVIEW_ALLOWED_HOSTS": "*", "LAW_REVIEW_API_TOKENS_JSON": ""},
        ):
            report = run_checks(self.data)
        checks = self.checks_by_name(report)
        self.assertEqual(checks["allowed_hosts"]["status"], "fail")
        self.assertEqual(checks["administrator"]["status"], "fail")

    def test_schema_mismatch_is_reported(self):
        with closing(sqlite3.connect(self.data / "law_review.db")) as conn:
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        checks = self.checks_by_name(run_checks(self.data))
        self.assertEqual(checks["schema_version"]["status"], "fail")
        self.assertIn("期望", checks["schema_version"]["detail"])

    def test_backup_drill_requires_quiescent_and_restores_identical_counts(self):
        target = Path(self.directory.name) / "drill"
        skipped = self.checks_by_name(run_checks(self.data, backup_target=target, quiescent=False))
        self.assertEqual(skipped["backup_drill"]["status"], "fail")
        self.assertIn("--quiescent", skipped["backup_drill"]["detail"])

        with closing(sqlite3.connect(self.data / "law_review.db")) as conn:
            conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('验收案件','2026-10-27T00:00:00+08:00','2026-10-27T00:00:00+08:00')"
            )
            conn.commit()
        drill = Path(self.directory.name) / "drill-ok"
        report = run_checks(self.data, backup_target=drill, quiescent=True)
        checks = self.checks_by_name(report)
        self.assertEqual(checks["backup_drill"]["status"], "pass", report)
        self.assertEqual(checks["restore_counts"]["status"], "pass", report)
        self.assertTrue((drill / "backup" / "manifest.json").is_file())
        self.assertTrue((drill / "restored" / "law_review.db").is_file())

    def test_stale_approved_report_is_flagged(self):
        from app.db import connect, now, transaction
        from app.report_basis import basis_hash, basis_snapshot

        with transaction() as conn:
            case_id = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('报告验收案',?,?)", (now(), now())
            ).lastrowid
            evidence_id = conn.execute(
                "INSERT INTO evidence(case_id,title,fact,status,created_at) VALUES (?,?,?,'已确认',?)",
                (case_id, "借条", "借款十万元", now()),
            ).lastrowid
        with connect() as conn:
            basis = basis_snapshot(conn, case_id)
        with transaction() as conn:
            conn.execute(
                "INSERT INTO case_reports(case_id,version,title,status,basis_json,basis_hash,prepared_by,"
                "approved_by,created_at,updated_at) VALUES (?,1,'结案报告','已批准',?,?,?,?,?,?)",
                (case_id, json.dumps(basis, ensure_ascii=False), basis_hash(basis), "管理员", "管理员", now(), now()),
            )
            conn.execute("UPDATE evidence SET version = version + 1 WHERE id = ?", (evidence_id,))
        checks = self.checks_by_name(run_checks(self.data))
        self.assertEqual(checks["report_basis"]["status"], "warn")
        self.assertIn("依据已变化", checks["report_basis"]["detail"])

    def test_tokens_never_appear_in_output(self):
        report = run_checks(self.data)
        self.assertNotIn(ADMIN_TOKEN, json.dumps(report, ensure_ascii=False))

    def test_local_mode_warns_instead_of_crashing(self):
        with patch.dict(os.environ, {"LAW_REVIEW_AUTH_MODE": "local"}):
            report = run_checks(self.data)
        checks = self.checks_by_name(report)
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(checks["multi_user"]["status"], "warn")

    def test_multi_user_profile_rejects_local_mode(self):
        with patch.dict(os.environ, {"LAW_REVIEW_AUTH_MODE": "local"}):
            report = run_checks(self.data, multi_user=True)
        checks = self.checks_by_name(report)
        self.assertEqual(checks["multi_user"]["status"], "fail")
        self.assertIn("不能作为多人交付验收结论", checks["multi_user"]["detail"])
        self.assertGreaterEqual(report["summary"]["fail"], 1)

    def test_inspection_never_creates_a_database(self):
        missing = Path(self.directory.name) / "wrong-path"
        missing.mkdir()
        report = run_checks(missing)
        checks = self.checks_by_name(report)
        self.assertEqual(checks["database"]["status"], "fail")
        self.assertFalse((missing / "law_review.db").exists())

    def test_model_probe_requires_a_listed_target_model(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def read(self, _limit=None):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        empty = run_checks(self.data, model_url="http://127.0.0.1:9000/v1")
        self.assertEqual(self.checks_by_name(empty)["model_endpoint"]["status"], "fail")
        with patch("scripts.collaboration_acceptance.urllib.request.urlopen",
                   return_value=Response({"data": []})):
            no_models = self.checks_by_name(run_checks(self.data, model_url="http://127.0.0.1:9000/v1"))
        self.assertEqual(no_models["model_endpoint"]["status"], "pass")
        self.assertEqual(no_models["model_list"]["status"], "fail")
        with patch("scripts.collaboration_acceptance.urllib.request.urlopen",
                   return_value=Response({"data": [{"id": "qwen3-32b"}]})):
            missing_target = self.checks_by_name(
                run_checks(self.data, model_url="http://127.0.0.1:9000/v1", model_id="other-model")
            )
            found = self.checks_by_name(
                run_checks(self.data, model_url="http://127.0.0.1:9000/v1", model_id="qwen3-32b")
            )
        self.assertEqual(missing_target["model_target"]["status"], "fail")
        self.assertEqual(found["model_target"]["status"], "pass")
        self.assertEqual(found["model_inference"]["status"], "skip")

    def _approved_frozen_report(self):
        from app.db import now, transaction
        from app.report_delivery import freeze_for_submission

        with transaction() as conn:
            case_id = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES ('冻结验收案',?,?)", (now(), now())
            ).lastrowid
            report_id = conn.execute(
                "INSERT INTO case_reports(case_id,version,title,status,prepared_by,created_at,updated_at,row_version) "
                "VALUES (?,1,'结案报告','草稿','管理员',?,?,1)",
                (case_id, now(), now()),
            ).lastrowid
            row = conn.execute("SELECT * FROM case_reports WHERE id = ?", (report_id,)).fetchone()
            frozen = freeze_for_submission(conn, row, actor="管理员")
            conn.execute(
                "UPDATE case_reports SET status='已批准', approved_by='管理员', approved_version_id=?, "
                "content_hash=?, source_hash=? WHERE id=?",
                (frozen["id"], frozen["content_hash"], frozen["source_hash"], report_id),
            )
        return case_id, report_id, frozen

    def test_backup_drill_verifies_frozen_report_artifacts(self):
        case_id, report_id, frozen = self._approved_frozen_report()
        report = run_checks(
            self.data, backup_target=Path(self.directory.name) / "drill-frozen", quiescent=True
        )
        checks = self.checks_by_name(report)
        self.assertEqual(checks["backup_drill"]["status"], "pass", report)
        self.assertEqual(checks["restore_integrity"]["status"], "pass", report)
        self.assertEqual(checks["restore_artifacts"]["status"], "pass", report)
        self.assertEqual(checks["restore_counts"]["status"], "pass", report)
        self.assertIn("耗时", checks["backup_drill"]["detail"])

        artifact_dir = self.data / frozen["artifact_dir"]
        victim = sorted(path for path in artifact_dir.iterdir() if path.name.startswith("001-"))[0]
        victim.unlink()
        broken = run_checks(
            self.data, backup_target=Path(self.directory.name) / "drill-broken", quiescent=True
        )
        broken_checks = self.checks_by_name(broken)
        self.assertEqual(broken_checks["restore_artifacts"]["status"], "fail", broken)
        self.assertIn("缺少冻结文件", broken_checks["restore_artifacts"]["detail"])

    def test_old_schema_tables_are_classified_not_crashed(self):
        with closing(sqlite3.connect(self.data / "law_review.db")) as conn:
            for table in ("case_report_versions", "case_report_events", "bank_transactions"):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("PRAGMA user_version = 24")
            conn.commit()
        report = run_checks(
            self.data, backup_target=Path(self.directory.name) / "drill-old", quiescent=True
        )
        checks = self.checks_by_name(report)
        self.assertEqual(checks["backup_drill"]["status"], "pass", report)
        self.assertEqual(checks["restore_schema"]["status"], "warn")
        self.assertIn("case_report_versions", checks["restore_schema"]["detail"])
        self.assertEqual(checks["restore_artifacts"]["status"], "pass", report)


if __name__ == "__main__":
    unittest.main()
