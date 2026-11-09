"""Frozen delivery body: approval and export read one immutable version.

Covers R2 acceptance cases: preview/submit/approve/export on one version and
hash, template and QA invalidation, material changes between steps, concurrent
state changes during export, more than 500 transaction rows with mixed
currencies, missing attachments, interrupted freezes and pre-freeze approvals.
"""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.storage.db import connect, db_scope, init_db, now, transaction


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FrozenDeliveryCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-frozen-")
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        scope = db_scope(self.data_dir / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    # --- helpers -----------------------------------------------------------

    def create_case(self, title="冻结测试案"):
        response = self.client.post("/api/cases", json={"title": title})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def upload(self, case_id, name="借条.txt", text="今借到人民币十万元整。".encode()):  # noqa: B008 - bytes are immutable
        from app.documents.ingest import index_upload
        return index_upload(case_id, name, text, "text/plain")

    def evidence(self, case_id, title="借条原件", status="已确认"):
        response = self.client.post(
            f"/api/cases/{case_id}/evidence",
            json={"title": title, "category": "书证", "fact": "借款十万元", "quote": "今借到人民币十万元", "status": status},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["evidence"]

    def template(self, blocks=None, name="冻结模板"):
        response = self.client.post(
            "/api/export-templates",
            json={"name": name, "blocks": blocks or [
                {"type": "case_summary", "title": "案件摘要"},
                {"type": "catalog_csv", "title": "内容级目录"},
                {"type": "evidence_table", "title": "证据目录"},
                {"type": "attachments", "title": "原始卷宗"},
            ]},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def report(self, case_id, template_id=None):
        payload = {"title": "结案报告"}
        if template_id is not None:
            payload["template_id"] = template_id
        response = self.client.post(f"/api/cases/{case_id}/reports", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["report"]

    def act(self, report_id, action, **extra):
        return self.client.patch(f"/api/reports/{report_id}", json={"action": action, **extra})

    def manifest(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("清单.json"))
            files = {name: archive.read(name) for name in archive.namelist()}
        return manifest, files


class FrozenVersionTest(FrozenDeliveryCase):
    def test_preview_approval_and_export_share_one_version(self):
        case_id = self.create_case()
        self.evidence(case_id)
        self.upload(case_id)
        template_id = self.template()
        report = self.report(case_id, template_id)

        draft = self.client.get(f"/api/reports/{report['id']}/frozen")
        self.assertEqual(draft.status_code, 404)

        submitted = self.act(report["id"], "submit")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        frozen = submitted.json()["report"]
        self.assertEqual(frozen["status"], "待复核")
        self.assertTrue(frozen["frozen_version_id"])
        self.assertTrue(frozen["content_hash"])

        preview = self.client.get(f"/api/reports/{report['id']}/frozen")
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        self.assertEqual(body["report"]["content_hash"], frozen["content_hash"])
        self.assertEqual({block["type"] for block in body["blocks"]},
                         {"case_summary", "catalog_csv", "evidence_table"})
        self.assertEqual(len(body["attachments"]), 1)
        attachment_sha = body["attachments"][0]["sha256"]

        approved = self.act(report["id"], "approve")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["report"]["approved_version_id"], frozen["frozen_version_id"])

        response = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        manifest, files = self.manifest(response)
        self.assertTrue(manifest["finalized"])
        self.assertEqual(manifest["report"]["id"], report["id"])
        self.assertEqual(manifest["report"]["approved_by"], "本机律师")
        self.assertEqual(manifest["report"]["content_hash"], frozen["content_hash"])
        self.assertEqual(manifest["frozen"]["content_hash"], frozen["content_hash"])
        self.assertEqual(manifest["frozen"]["version_id"], frozen["frozen_version_id"])
        self.assertEqual(manifest["files"][0]["sha256"], attachment_sha)
        self.assertEqual(manifest["files"][0]["sha256"], sha256(files[manifest["files"][0]["archive_name"]]))
        # The archived body is the reviewed text, byte for byte.
        summary = files["案件摘要.md"].decode("utf-8")
        frozen_block = next(block for block in body["blocks"] if block["type"] == "case_summary")
        self.assertEqual(sha256(summary.encode("utf-8")), frozen_block["sha256"])
        self.assertIn("借条原件", files["证据目录.csv"].decode("utf-8-sig"))

    def test_template_body_or_static_text_change_requires_a_new_version(self):
        case_id = self.create_case()
        self.evidence(case_id)
        template_id = self.template([
            {"type": "case_summary", "title": "案件摘要"},
            {"type": "evidence_table", "title": "证据目录"},
            {"type": "static_markdown", "title": "说明", "content": "初稿说明"},
        ])
        report = self.report(case_id, template_id)
        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        self.assertEqual(self.act(report["id"], "approve").status_code, 200)

        updated = self.client.put(
            f"/api/export-templates/{template_id}",
            json={"name": "冻结模板", "blocks": [
                {"type": "case_summary", "title": "案件摘要"},
                {"type": "evidence_table", "title": "证据目录"},
                {"type": "static_markdown", "title": "说明", "content": "修改后的说明"},
            ]},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        blocked = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("已变化", blocked.text)

        listed = self.client.get(f"/api/cases/{case_id}/reports").json()
        self.assertTrue(listed["reports"][0]["stale"])
        self.assertFalse(listed["deliverable"])

        # A new version freezes the new template content and delivers again.
        second = self.report(case_id, template_id)
        self.assertEqual(self.act(second["id"], "submit").status_code, 200)
        self.assertEqual(self.act(second["id"], "approve").status_code, 200)
        manifest, files = self.manifest(
            self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={second['id']}")
        )
        self.assertEqual(manifest["report"]["version"], 2)
        self.assertEqual(files["说明.md"].decode("utf-8"), "修改后的说明")

    def test_qa_log_is_frozen_and_blocks_a_late_question(self):
        case_id = self.create_case()
        self.evidence(case_id)
        template_id = self.template([
            {"type": "case_summary", "title": "案件摘要"},
            {"type": "qa_log", "title": "阅卷问答记录"},
        ])
        with transaction() as conn:
            conversation_id = conn.execute(
                "INSERT INTO conversations(case_id, title, user_name, created_at) VALUES (?,?,?,?)",
                (case_id, "阅卷问答", "承办律师", now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                (conversation_id, "user", "第一次提问", now()),
            )
        report = self.report(case_id, template_id)
        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        self.assertEqual(self.act(report["id"], "approve").status_code, 200)
        manifest, files = self.manifest(
            self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        )
        self.assertIn("第一次提问", files["阅卷问答记录.md"].decode("utf-8"))

        with transaction() as conn:
            conn.execute(
                "INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                (conversation_id, "user", "批准后追加的提问", now()),
            )
        blocked = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("已变化", blocked.text)

    def test_material_change_between_freeze_and_approval_is_rejected(self):
        case_id = self.create_case()
        evidence = self.evidence(case_id)
        report = self.report(case_id, self.template())
        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        self.client.patch(f"/api/evidence/{evidence['id']}", json={"fact": "更正为二十万元"})
        blocked = self.act(report["id"], "approve")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("已变化", blocked.json()["detail"])
        status = self.client.get(f"/api/cases/{case_id}/reports").json()["reports"][0]
        self.assertEqual(status["status"], "待复核")
        self.assertTrue(status["stale"])

    def test_template_cannot_be_swapped_for_a_formal_export(self):
        case_id = self.create_case()
        self.evidence(case_id)
        other = self.template([{"type": "case_summary", "title": "案件摘要"}], name="另一个模板")
        report = self.report(case_id, self.template())
        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        self.assertEqual(self.act(report["id"], "approve").status_code, 200)
        blocked = self.client.get(f"/api/cases/{case_id}/export?final=true&template_id={other}")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("不能替换", blocked.text)

    def test_legacy_approval_without_frozen_body_cannot_be_delivered(self):
        case_id = self.create_case()
        with transaction() as conn:
            report_id = conn.execute(
                "INSERT INTO case_reports(case_id, version, title, status, prepared_by, reviewed_by, approved_by, "
                "created_at, updated_at, row_version) VALUES (?,1,'历史报告','已批准','旧律师','旧复核','旧复核',?,?,1)",
                (case_id, now(), now()),
            ).lastrowid
        blocked = self.client.get(f"/api/cases/{case_id}/export?final=true")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("没有冻结交付正文", blocked.text)
        self.assertEqual(
            self.client.get(f"/api/reports/{report_id}/frozen").status_code, 404
        )


class FrozenIntegrityTest(FrozenDeliveryCase):
    def _transaction_rows(self, case_id, count=620, currencies=("CNY", "USD")):
        rows = []
        for index in range(1, count + 1):
            rows.append((
                index, "账户甲" if index % 2 else "账户乙", "inflow" if index % 3 else "outflow",
                index, currencies[index % len(currencies)], "2024-01-02", "对方丙",
            ))
        with transaction() as conn:
            conn.executemany(
                "INSERT INTO bank_transactions(case_id, source_row_number, account, direction, amount_minor, "
                "currency, transaction_time, counterparty, memo, parser_version, row_fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,'备注','test',?,?)",
                [(case_id, row[0], row[1], row[2], row[3], row[4], row[5], row[6], f"fp-{row[0]}", now())
                 for row in rows],
            )
        return rows

    def test_full_transaction_sheet_is_frozen_and_delivered(self):
        case_id = self.create_case()
        rows = self._transaction_rows(case_id)
        report = self.report(case_id)
        submitted = self.act(report["id"], "submit")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        preview = self.client.get(f"/api/reports/{report['id']}/frozen").json()
        self.assertEqual(preview["transactions"]["count"], len(rows))
        self.assertEqual(sorted(preview["transactions"]["currencies"]), ["CNY", "USD"])
        self.assertEqual(self.act(report["id"], "approve").status_code, 200)
        manifest, files = self.manifest(
            self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        )
        sheet = files["资金流水.csv"]
        self.assertTrue(sheet.startswith("\ufeff".encode()))
        self.assertEqual(manifest["artifacts"][0]["transaction_count"], len(rows))
        self.assertEqual(manifest["artifacts"][0]["sha256"], sha256(sheet))
        self.assertEqual(len(sheet.decode("utf-8-sig").strip().splitlines()), len(rows) + 1)

        # A later transaction row makes the approved bundle stale.
        with transaction() as conn:
            conn.execute(
                "INSERT INTO bank_transactions(case_id, source_row_number, account, direction, amount_minor, "
                "currency, transaction_time, counterparty, memo, parser_version, row_fingerprint, created_at) "
                "VALUES (?,9999,'账户丁','inflow',1,'CNY','2024-02-01','对方丁','备注','test','fp-new',?)",
                (case_id, now()),
            )
        blocked = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        self.assertEqual(blocked.status_code, 400)

    def test_hash_only_sheet_matches_rendered_bytes_at_chunk_boundaries(self):
        from app.cases.report_delivery import transaction_sheet

        for count in (0, 500, 620):
            with self.subTest(count=count):
                case_id = self.create_case()
                self._transaction_rows(case_id, count=count)
                rendered = transaction_sheet(case_id)
                hashed = transaction_sheet(case_id, include_text=False)
                self.assertNotIn("text", hashed)
                self.assertEqual(hashed["count"], count)
                self.assertEqual(hashed["sha256"], sha256(rendered["text"].encode("utf-8")))
                self.assertEqual(hashed["sha256"], rendered["sha256"])
                self.assertEqual(hashed["currencies"], rendered["currencies"])

    def test_missing_attachment_blocks_freeze_and_delivery(self):
        case_id = self.create_case()
        document = self.upload(case_id)
        template_id = self.template([
            {"type": "case_summary", "title": "案件摘要"},
            {"type": "attachments", "title": "原始卷宗", "required": True},
        ])
        report = self.report(case_id, template_id)
        stored = Path(document["stored_path"])
        original = stored.read_bytes()
        stored.unlink()
        blocked = self.act(report["id"], "submit")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("附件缺失", blocked.text)
        self.assertEqual(
            self.client.get(f"/api/cases/{case_id}/reports").json()["reports"][0]["status"], "草稿"
        )

        stored.write_bytes(original)
        optional = self.template([
            {"type": "case_summary", "title": "案件摘要"},
            {"type": "attachments", "title": "原始卷宗"},
        ], name="可选附件模板")
        optional_report = self.report(case_id, optional)
        stored.unlink()
        strict = self.act(optional_report["id"], "submit")
        self.assertEqual(strict.status_code, 400)
        self.assertIn("附件缺失", strict.text)
        stored.write_bytes(original)

        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        self.assertEqual(self.act(report["id"], "approve").status_code, 200)
        stored.write_bytes(original + b"tampered")
        delivery = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        self.assertEqual(delivery.status_code, 400)
        self.assertIn("附件内容已变化", delivery.text)

    def test_interrupted_freeze_artifact_blocks_approval_and_download(self):
        case_id = self.create_case()
        self.evidence(case_id)
        report = self.report(case_id)
        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        frozen_id = self.client.get(f"/api/cases/{case_id}/reports").json()["reports"][0]["frozen_version_id"]
        with closing(connect()) as conn:
            artifact_dir = conn.execute(
                "SELECT artifact_dir FROM case_report_versions WHERE id = ?", (frozen_id,)
            ).fetchone()[0]
        target = self.data_dir / artifact_dir
        victim = sorted(path for path in target.iterdir() if path.name.startswith("001-"))[0]
        victim.unlink()

        blocked = self.act(report["id"], "approve")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("冻结正文文件缺失", blocked.json()["detail"])
        preview = self.client.get(f"/api/reports/{report['id']}/frozen")
        self.assertEqual(preview.status_code, 409)

        # A failed artifact can never be delivered even if the row says approved.
        with transaction() as conn:
            conn.execute(
                "UPDATE case_reports SET status='已批准', approved_by='复核律师', approved_version_id=? WHERE id=?",
                (frozen_id, report["id"]),
            )
        delivery = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        self.assertEqual(delivery.status_code, 400)
        self.assertIn("冻结正文文件缺失", delivery.text)

    def test_relocating_stored_paths_keeps_approval_valid(self):
        case_id = self.create_case()
        document = self.upload(case_id)
        template_id = self.template([
            {"type": "case_summary", "title": "案件摘要"},
            {"type": "attachments", "title": "原始卷宗", "required": True},
        ])
        report = self.report(case_id, template_id)
        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        self.assertEqual(self.act(report["id"], "approve").status_code, 200)

        moved_dir = self.data_dir / "restored-uploads"
        moved_dir.mkdir()
        moved = moved_dir / Path(document["stored_path"]).name
        moved.write_bytes(Path(document["stored_path"]).read_bytes())
        Path(document["stored_path"]).unlink()
        with transaction() as conn:
            conn.execute("UPDATE documents SET stored_path = ? WHERE id = ?", (str(moved), document["id"]))

        manifest, files = self.manifest(
            self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        )
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(manifest["files"][0]["sha256"], sha256(files[manifest["files"][0]["archive_name"]]))


class FrozenConcurrencyTest(FrozenDeliveryCase):
    def test_basis_changes_during_packaging_refuse_delivery_registration(self):
        from app.cases import report_delivery

        original = report_delivery._write_package
        for change in ("evidence", "page", "transactions"):
            with self.subTest(change=change):
                case_id = self.create_case()
                evidence = self.evidence(case_id)
                document = self.upload(case_id)
                report = self.report(case_id)
                self.assertEqual(self.act(report["id"], "submit").status_code, 200)
                self.assertEqual(self.act(report["id"], "approve").status_code, 200)

                def race(*args, **kwargs):
                    package_sha = original(*args, **kwargs)
                    # A distinct connection commits after source validation and
                    # ZIP assembly, before the final delivery transaction.
                    with transaction() as conn:
                        if change == "evidence":
                            conn.execute(
                                "UPDATE evidence SET fact='并发更正的待证事实', version=version+1 WHERE id=?",
                                (evidence["id"],),
                            )
                        elif change == "page":
                            conn.execute(
                                "UPDATE pages SET text='并发更正的卷宗原文' WHERE document_id=? AND page_no=1",
                                (document["id"],),
                            )
                            conn.execute("UPDATE documents SET version=version+1 WHERE id=?", (document["id"],))
                        else:
                            conn.execute(
                                "INSERT INTO bank_transactions(case_id, source_row_number, account, direction, "
                                "amount_minor, currency, transaction_time, counterparty, memo, parser_version, "
                                "row_fingerprint, created_at) "
                                "VALUES (?,2,'合成账户','inflow',1200,'CNY','2026-01-02','合成对手','并发新增',"
                                "'test','race-new',?)",
                                (case_id, now()),
                            )
                    return package_sha

                with patch.object(report_delivery, "_write_package", side_effect=race):
                    refused = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
                self.assertEqual(refused.status_code, 400, refused.text)
                self.assertIn("导出期间已变化", refused.text)
                self.assertIn("已拒绝本次交付", refused.text)
                listed = self.client.get(f"/api/cases/{case_id}/reports").json()
                self.assertTrue(listed["reports"][0]["stale"])
                self.assertFalse(listed["deliverable"])
                with closing(connect()) as conn:
                    rows = conn.execute(
                        "SELECT COUNT(*) FROM audit_log WHERE case_id=? AND action='正式交付登记'", (case_id,),
                    ).fetchone()[0]
                self.assertEqual(rows, 0)

    def test_state_change_during_export_is_refused_at_registration(self):
        case_id = self.create_case()
        self.evidence(case_id)
        report = self.report(case_id)
        self.assertEqual(self.act(report["id"], "submit").status_code, 200)
        self.assertEqual(self.act(report["id"], "approve").status_code, 200)

        from app.cases import report_delivery

        original = report_delivery._write_package

        def race(*args, **kwargs):
            # A second connection voids the report after the ZIP bytes exist but
            # before the delivery is registered.
            with transaction() as conn:
                conn.execute(
                    "UPDATE case_reports SET status='已作废', voided_at=?, updated_at=? WHERE id=?",
                    (now(), now(), report["id"]),
                )
            return original(*args, **kwargs)

        with patch.object(report_delivery, "_write_package", side_effect=race):
            refused = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("已拒绝本次交付", refused.text)

        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = '正式交付登记'"
            ).fetchone()[0]
        self.assertEqual(rows, 0)

    def test_delivery_is_registered_once_with_both_digests(self):
        case_id = self.create_case()
        report = self.report(case_id)
        self.act(report["id"], "submit")
        self.act(report["id"], "approve")
        response = self.client.get(f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        package_sha = response.headers["X-Package-Sha256"]
        with closing(connect()) as conn:
            detail = conn.execute(
                "SELECT detail FROM audit_log WHERE action = '正式交付登记' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            frozen = conn.execute(
                "SELECT content_hash, source_hash FROM case_report_versions WHERE report_id = ?",
                (report["id"],),
            ).fetchone()
        self.assertIn(frozen["content_hash"], detail)
        self.assertIn(frozen["source_hash"], detail)
        self.assertIn(package_sha, detail)


if __name__ == "__main__":
    unittest.main()
