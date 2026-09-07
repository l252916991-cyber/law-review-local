import os
import tempfile
import unittest
from pathlib import Path

from tests.support import IsolatedDatabaseTestCase


class BankTransactionParserTest(unittest.TestCase):
    def test_csv_aliases_decimal_direction_and_utf8_bom(self):
        from app.bank_transactions import parse_csv
        payload = "\ufeff账号,收支方向,交易金额,交易日期,对方户名,摘要\nA,收入,\"1,234.50\",2024-01-02,B,款项".encode()
        rows = parse_csv(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].amount_minor, 123450)
        self.assertEqual(rows[0].direction, "inflow")
        self.assertEqual(rows[0].transaction_time, "2024-01-02")
        self.assertEqual(rows[0].parse_status, "parsed")

    def test_gb18030_and_needs_review_warnings(self):
        from app.bank_transactions import parse_csv
        payload = "账户,金额,日期,对方账户\n甲,坏金额,日期,乙\n".encode("gb18030")
        rows = parse_csv(payload)
        self.assertEqual(rows[0].parse_status, "needs_review")
        self.assertIn("invalid_amount", rows[0].warnings)
        self.assertIn("invalid_date", rows[0].warnings)
        self.assertIn("missing_direction", rows[0].warnings)

    def test_limits_rows_and_cells(self):
        from app.bank_transactions import parse_csv
        with self.assertRaises(ValueError):
            parse_csv(("账号,金额\n" + "a,1\n" * 100001).encode())


class BankTransactionServiceTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.db import init_db
        init_db(seed=True)

    def test_persist_summary_graph_and_replay_are_exact(self):
        from app.db import connect
        from app.services import bank_transaction_graph, list_bank_transactions, persist_bank_transactions, summarize_bank_transactions
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
        from app.db import connect, now, transaction
        with transaction() as conn:
            case_id = conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('流水案件',?,?)", (now(), now())).lastrowid
            conn.execute("INSERT INTO bank_transactions(case_id,source_row_number,account,parser_version,row_fingerprint,created_at) VALUES (?,?,?,?,?,?)", (case_id, 2, "A", "test", "fingerprint", now()))
        with transaction() as conn:
            conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE case_id=?", (case_id,)).fetchone()[0], 0)
