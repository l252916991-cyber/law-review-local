import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.support import IsolatedDatabaseTestCase


class BankTransactionParserTest(unittest.TestCase):
    def test_csv_aliases_decimal_direction_and_utf8_bom(self):
        from app.documents.bank_transactions import parse_csv
        payload = "\ufeff账号,收支方向,交易金额,交易日期,对方户名,摘要\nA,收入,\"1,234.50\",2024-01-02,B,款项".encode()
        rows = parse_csv(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].amount_minor, 123450)
        self.assertEqual(rows[0].direction, "inflow")
        self.assertEqual(rows[0].transaction_time, "2024-01-02")
        self.assertEqual(rows[0].parse_status, "parsed")

    def test_gb18030_and_needs_review_warnings(self):
        from app.documents.bank_transactions import parse_csv
        payload = "账户,金额,日期,对方账户\n甲,坏金额,日期,乙\n".encode("gb18030")
        rows = parse_csv(payload)
        self.assertEqual(rows[0].parse_status, "needs_review")
        self.assertIn("invalid_amount", rows[0].warnings)
        self.assertIn("invalid_date", rows[0].warnings)
        self.assertIn("missing_direction", rows[0].warnings)

    def test_limits_rows_and_cells(self):
        from app.documents.bank_transactions import parse_csv
        with self.assertRaises(ValueError):
            parse_csv(("账号,金额\n" + "a,1\n" * 100001).encode())

    def test_xlsx_uses_value_only_parser_and_keeps_sheet_provenance(self):
        from datetime import date

        from openpyxl import Workbook

        from app.documents.bank_transactions import parse_spreadsheet

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "流水"
        sheet.append(["账号", "收支方向", "交易金额", "交易日期", "对方户名", "摘要"])
        sheet.append(["A", "收入", 12.5, date(2024, 1, 2), "B", "入账"])
        formula_sheet = workbook.create_sheet("公式")
        formula_sheet.append(["账号", "收支方向", "交易金额", "交易日期", "对方户名"])
        formula_sheet.append(["A", "收入", "=1+1", date(2024, 1, 3), "C"])
        payload = io.BytesIO()
        workbook.save(payload)

        rows = parse_spreadsheet(payload.getvalue(), "流水.xlsx")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].source_sheet, "流水")
        self.assertEqual(rows[0].amount_minor, 1250)
        self.assertEqual(rows[0].transaction_time, "2024-01-02")
        # Formula cells are not executed; without a cached value they remain a
        # review item instead of being treated as a calculated amount.
        self.assertIsNone(rows[1].amount_minor)
        self.assertIn("missing_amount_value", rows[1].warnings)

    def test_spreadsheet_payload_limit_is_checked_before_parsing(self):
        from app.documents.bank_transactions import MAX_PAYLOAD_BYTES, parse_spreadsheet

        with self.assertRaises(ValueError):
            parse_spreadsheet(b"x" * (MAX_PAYLOAD_BYTES + 1), "流水.xlsx")

    def test_malformed_spreadsheet_is_a_bounded_value_error(self):
        from app.documents.bank_transactions import parse_spreadsheet

        with self.assertRaisesRegex(ValueError, "无法解析"):
            parse_spreadsheet(b"not-a-workbook", "流水.xlsx")


class BankTransactionServiceTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.storage.db import init_db
        init_db(seed=True)

    def test_persist_summary_graph_and_replay_are_exact(self):
        from app.review.services import (
            bank_transaction_graph,
            list_bank_transactions,
            persist_bank_transactions,
            summarize_bank_transactions,
        )
        from app.storage.db import connect
        payload = "账号,收支方向,交易金额,交易日期,对方户名,摘要\nA,收入,100.00,2024-01-02,B,入账\nA,支出,20.00,2024-01-03,C,付款\n".encode()
        with tempfile.TemporaryDirectory(prefix="bank-source-") as directory:
            source = Path(directory) / "流水.csv"
            source.write_bytes(payload)
            with connect() as conn:
                document_id = conn.execute("SELECT id FROM documents WHERE case_id=1 LIMIT 1").fetchone()[0]
                source_hash = conn.execute("SELECT content_hash FROM documents WHERE id=?", (document_id,)).fetchone()[0] or "source-hash"
            first = persist_bank_transactions(1, document_id, source_hash, payload)
            second = persist_bank_transactions(1, document_id, source_hash, payload)
        self.assertEqual(first["rows"], 2)
        self.assertEqual(second["rows"], 2)
        rows = list_bank_transactions(1)["transactions"]
        self.assertEqual(len(rows), 2)
        summary = summarize_bank_transactions(1)
        self.assertEqual(summary["inflow_minor"], 10000)
        self.assertEqual(summary["outflow_minor"], 2000)
        self.assertEqual(summary["net_minor"], 8000)
        graph = bank_transaction_graph(1)
        self.assertEqual(graph["transaction_count"], 2)
        self.assertTrue(all(edge["relation_type"] == "资金链路" for edge in graph["edges"]))
        self.assertTrue(any(edge["from"] == "party:B" and edge["to"] == "account:A" for edge in graph["edges"]))
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE case_id=1").fetchone()[0], 2)

    def test_case_delete_cascades_transactions(self):
        from app.storage.db import connect, now, transaction
        with transaction() as conn:
            case_id = conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('流水案件',?,?)", (now(), now())).lastrowid
            conn.execute("INSERT INTO bank_transactions(case_id,source_row_number,account,parser_version,row_fingerprint,created_at) VALUES (?,?,?,?,?,?)", (case_id, 2, "A", "test", "fingerprint", now()))
        with transaction() as conn:
            conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE case_id=?", (case_id,)).fetchone()[0], 0)

    def test_xlsx_upload_is_indexed_and_persisted_with_sheet_reference(self):
        from datetime import date

        from openpyxl import Workbook

        from app.review.services import index_upload, parse_spreadsheet, persist_parsed_bank_rows
        from app.storage.db import connect

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "主表"
        sheet.append(["账号", "收支方向", "交易金额", "交易日期", "对方户名"])
        sheet.append(["A", "支出", 20, date(2024, 2, 1), "C"])
        payload = io.BytesIO()
        workbook.save(payload)
        content = payload.getvalue()

        document = index_upload(1, "流水.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        rows = parse_spreadsheet(content, "流水.xlsx")
        result = persist_parsed_bank_rows(1, document["id"], document["content_hash"], rows)

        self.assertEqual(document["pages"], 1)
        self.assertEqual(result["parsed"], 1)
        with connect() as conn:
            stored = conn.execute("SELECT source_sheet, amount_minor, direction FROM bank_transactions WHERE source_document_id=?", (document["id"],)).fetchone()
        self.assertEqual(tuple(stored), ("主表", 2000, "outflow"))


class BankTransactionVolumeTest(IsolatedDatabaseTestCase):
    """统计、图谱与导出必须覆盖全部流水，而不是交互列表的第一页。"""

    @classmethod
    def setUpClass(cls):
        from app.storage.db import init_db
        init_db(seed=False)

    def test_summary_graph_and_export_cover_every_row(self):
        import zipfile

        from app.review.services import (
            bank_transaction_graph,
            build_export_package,
            list_bank_transactions,
            summarize_bank_transactions,
        )
        from app.storage.db import connect, now, transaction

        total = 620
        rows = [
            (
                index,
                "账户甲" if index % 2 else "账户乙",
                "inflow" if index % 3 else "outflow",
                index,
                "2024-01-02",
                "对方丙",
            )
            for index in range(1, total + 1)
        ]
        with transaction() as conn:
            case_id = conn.execute(
                "INSERT INTO cases(title,case_no,created_at,updated_at) VALUES ('大额流水案','(2026)测1号',?,?)",
                (now(), now()),
            ).lastrowid
            conn.executemany(
                "INSERT INTO bank_transactions(case_id,source_row_number,account,direction,amount_minor,currency,transaction_time,counterparty,memo,parser_version,row_fingerprint,created_at) "
                "VALUES (?,?,?,?,?,'CNY',?,?,'备注','test',?,?)",
                [(case_id, row[0], row[1], row[2], row[3], row[4], row[5], f"fp-{row[0]}", now()) for row in rows],
            )

        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE case_id=?", (case_id,)).fetchone()[0], total)

        page = list_bank_transactions(case_id)
        self.assertEqual(page["pagination"]["total"], total)
        self.assertEqual(len(page["transactions"]), 100)

        inflow = [row for row in rows if row[2] == "inflow"]
        outflow = [row for row in rows if row[2] == "outflow"]
        summary = summarize_bank_transactions(case_id)
        self.assertEqual(summary["transaction_count"], total)
        self.assertEqual(summary["inflow_count"], len(inflow))
        self.assertEqual(summary["outflow_count"], len(outflow))
        self.assertEqual(summary["inflow_minor"], sum(row[3] for row in inflow))
        self.assertEqual(summary["outflow_minor"], sum(row[3] for row in outflow))
        self.assertEqual(summary["net_minor"], sum(row[3] for row in inflow) - sum(row[3] for row in outflow))
        self.assertEqual(summary["by_period"], {"2024-01": sum(row[3] for row in rows)})
        self.assertEqual(summary["by_counterparty"], {"对方丙": sum(row[3] for row in rows)})

        graph = bank_transaction_graph(case_id)
        self.assertEqual(graph["transaction_count"], total)
        self.assertEqual(sum(edge["count"] for edge in graph["edges"]), total)
        self.assertEqual(sum(edge["amount_minor"] for edge in graph["edges"]), sum(row[3] for row in rows))

        path, package_sha = build_export_package(case_id)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertIn("资金流水.csv", names)
            content = archive.read("资金流水.csv")
            self.assertTrue(content.startswith("\ufeff".encode()))
            lines = content.decode("utf-8-sig").splitlines()
            self.assertEqual(len(lines), total + 1)
            self.assertTrue(lines[1].startswith("2024-01-02,账户甲,inflow,"))
            manifest = json.loads(archive.read("清单.json"))
            artifact = manifest["artifacts"][0]
            self.assertEqual(artifact["transaction_count"], total)
            self.assertEqual(artifact["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(len(package_sha), 64)
