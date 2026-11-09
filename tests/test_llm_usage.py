"""Per-call model accounting: unknown usage must stay unknown, not zero."""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.llm.llm_usage import percentile, record_llm_call, token_counts, usage_summary
from app.storage.db import connect, db_scope, init_db, now, transaction


class TokenCountTests(unittest.TestCase):
    def test_reads_openai_and_local_naming(self):
        self.assertEqual(token_counts({"prompt_tokens": 12, "completion_tokens": 7}), (12, 7))
        self.assertEqual(token_counts({"input_tokens": 9, "output_tokens": 3}), (9, 3))

    def test_absent_or_malformed_usage_is_unknown_not_zero(self):
        for usage in (None, {}, "12", [], {"prompt_tokens": None}, {"prompt_tokens": -1}):
            with self.subTest(usage=usage):
                self.assertEqual(token_counts(usage), (None, None))

    def test_boolean_flag_never_becomes_a_token_count(self):
        self.assertEqual(token_counts({"prompt_tokens": True, "input_tokens": 5}), (5, None))


class PercentileTests(unittest.TestCase):
    def test_linear_interpolation(self):
        self.assertEqual(percentile([10, 20, 30, 40], 0.5), 25.0)
        self.assertEqual(percentile([10, 20, 30, 40], 0.95), 38.5)
        self.assertEqual(percentile([10], 0.95), 10.0)
        self.assertIsNone(percentile([], 0.5))

    def test_rejects_out_of_range_ratio(self):
        with self.assertRaises(ValueError):
            percentile([1, 2], 1.5)


class AccountingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-usage-")
        self.addCleanup(directory.cleanup)
        scope = db_scope(Path(directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case_id = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)",
                ("用量测试", now(), now()),
            ).lastrowid

    def test_migration_creates_the_accounting_table(self):
        with closing(
            __import__("app.storage.db", fromlist=["connect"]).connect()
        ) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)")}
        # The latest migration number is asserted on purpose: a schema bump must
        # update this test rather than silently passing on an old database.
        # v22 adds concurrency versions, v23 members, v24 review tasks,
        # v25 report signing, v26 frozen delivery bodies, v27 information walls, v28 conflict checks.
        self.assertEqual(version, 28)
        self.assertTrue({"case_id", "run_id", "input_tokens", "ttft_ms", "kind"} <= columns)

    def test_explicit_price_column_is_stored_verbatim(self):
        # The caller may have resolved the snapshot column itself (a declared
        # model spec carries it); accounting must not re-derive a different one.
        record_llm_call(case_id=self.case_id, kind="chat", model="undeclared-model",
                        usage={"prompt_tokens": 5, "completion_tokens": 1}, latency_ms=80,
                        price_column="snapshot-from-spec")
        with closing(connect()) as conn:
            stored = conn.execute("SELECT pricing_json FROM llm_calls").fetchone()[0]
        self.assertEqual(stored, "snapshot-from-spec")

    def test_absent_price_column_falls_back_to_the_model_lookup(self):
        record_llm_call(case_id=self.case_id, kind="chat", model="unknown-model",
                        usage={"prompt_tokens": 5, "completion_tokens": 1}, latency_ms=80)
        with closing(connect()) as conn:
            stored = conn.execute("SELECT pricing_json FROM llm_calls").fetchone()[0]
        # No pricing configuration in this isolated data directory: unknown, not zero.
        self.assertIsNone(stored)

    def test_unknown_usage_keeps_average_unknown(self):
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage=None, latency_ms=120)
        summary = usage_summary(self.case_id)
        self.assertEqual(summary["calls"], 1)
        self.assertIsNone(summary["input_tokens"])
        self.assertIsNone(summary["output_tokens"])
        # A measured latency still exists even when the token counts do not.
        self.assertEqual(summary["latency_p50_ms"], 120.0)

    def test_known_and_unknown_calls_are_not_averaged_together(self):
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage={"prompt_tokens": 10, "completion_tokens": 4}, latency_ms=100)
        record_llm_call(case_id=self.case_id, kind="critic", model="m", usage=None, latency_ms=300)
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage={"input_tokens": 20, "output_tokens": 6}, latency_ms=200)

        summary = usage_summary(self.case_id)
        self.assertEqual(summary["calls"], 3)
        self.assertEqual(summary["input_tokens"], 30)
        self.assertEqual(summary["output_tokens"], 10)
        self.assertEqual([row["kind"] for row in summary["by_kind"]], ["chat", "critic"])
        chat = next(row for row in summary["by_kind"] if row["kind"] == "chat")
        self.assertEqual(chat["calls"], 2)
        self.assertEqual(chat["input_tokens"], 30)

    def test_failures_and_ttft_are_reported(self):
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage=None, latency_ms=50, status="failed")
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage={"prompt_tokens": 1, "completion_tokens": 1}, latency_ms=400, ttft_ms=90)
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage={"prompt_tokens": 1, "completion_tokens": 1}, latency_ms=600, ttft_ms=110)

        summary = usage_summary(self.case_id)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["ttft_avg_ms"], 100.0)
        self.assertEqual(summary["ttft_p50_ms"], 100.0)
        self.assertAlmostEqual(summary["ttft_p95_ms"], 109.0)

    def test_empty_case_reports_zeros_without_inventing_costs(self):
        summary = usage_summary(self.case_id)
        self.assertEqual(summary["calls"], 0)
        self.assertIsNone(summary["input_tokens"])
        self.assertIsNone(summary["ttft_p95_ms"])
        # An empty sample has zero known measurements, which is not a measured 0.
        self.assertEqual(summary["input_tokens_known_calls"], 0)
        self.assertEqual(summary["output_tokens_known_calls"], 0)
        self.assertEqual(summary["ttft_known_calls"], 0)
        self.assertEqual(summary["by_model"], [])

    def test_coverage_counters_separate_partial_sums_from_totals(self):
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage={"prompt_tokens": 10, "completion_tokens": 4}, latency_ms=100, ttft_ms=40)
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage=None, latency_ms=300)
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage={"prompt_tokens": 20, "completion_tokens": 6}, latency_ms=200, ttft_ms=60)

        summary = usage_summary(self.case_id)
        self.assertEqual(summary["calls"], 3)
        self.assertEqual(summary["input_tokens"], 30)
        self.assertEqual(summary["input_tokens_known_calls"], 2)
        self.assertEqual(summary["output_tokens_known_calls"], 2)
        self.assertEqual(summary["ttft_known_calls"], 2)
        self.assertNotEqual(summary["input_tokens_known_calls"], summary["calls"])
        chat = next(row for row in summary["by_kind"] if row["kind"] == "chat")
        self.assertEqual(chat["calls"], 3)
        self.assertEqual(chat["failed"], 0)
        self.assertEqual(chat["input_tokens_known_calls"], 2)
        self.assertEqual(chat["ttft_known_calls"], 2)
        self.assertEqual(chat["ttft_p50_ms"], 50.0)

    def test_all_usage_missing_keeps_every_token_number_unknown(self):
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage=None, latency_ms=90)
        record_llm_call(case_id=self.case_id, kind="chat", model="m", usage={}, latency_ms=110, status="failed")

        summary = usage_summary(self.case_id)
        self.assertIsNone(summary["input_tokens"])
        self.assertEqual(summary["input_tokens_known_calls"], 0)
        self.assertEqual(summary["output_tokens_known_calls"], 0)
        self.assertEqual(summary["ttft_known_calls"], 0)
        # Latency is measured independently of the token accounting.
        self.assertEqual(summary["latency_p50_ms"], 100.0)
        self.assertEqual(summary["failed"], 1)
        model_row = summary["by_model"][0]
        self.assertEqual(model_row["failed"], 1)
        self.assertEqual(model_row["input_tokens_known_calls"], 0)
        self.assertIsNone(model_row["input_tokens"])


if __name__ == "__main__":
    unittest.main()
