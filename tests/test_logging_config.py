"""Operational logs stay isolated and avoid leaking raw exception details."""

import asyncio
import io
import json
import logging
import unittest

from app.logger import LogContext, StructuredFormatter, get_logger, setup_logging


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.handlers = list(self.root.handlers)
        self.level = self.root.level
        self.factory = logging.getLogRecordFactory()

    def tearDown(self):
        for handler in self.root.handlers[:]:
            if handler not in self.handlers:
                self.root.removeHandler(handler)
                handler.close()
        self.root.setLevel(self.level)
        self.assertIs(logging.getLogRecordFactory(), self.factory)

    def test_setup_is_idempotent_and_preserves_host_handlers(self):
        output = io.StringIO()
        setup_logging("INFO", stream=output)
        setup_logging("DEBUG", stream=output)
        managed = [handler for handler in self.root.handlers if handler.get_name() == "lexvault_structured"]
        self.assertEqual(len(managed), 1)
        self.assertTrue(all(handler in self.root.handlers for handler in self.handlers))
        get_logger("test").info("single_event")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())["message"], "single_event")

    def test_invalid_level_is_rejected_before_installing_handler(self):
        before = list(self.root.handlers)
        with self.assertRaises(ValueError):
            setup_logging("INVALID_LEVEL")
        self.assertEqual(self.root.handlers, before)

    def test_context_restores_nested_fields_and_drops_unknown_sensitive_fields(self):
        formatter = StructuredFormatter()
        logger = get_logger("test")
        record = logging.LogRecord("event", logging.INFO, "file", 1, "done", (), None)
        with LogContext(logger, case_id=1, token="secret-never-log"):
            self.assertEqual(json.loads(formatter.format(record))["case_id"], 1)
            with LogContext(logger, case_id=2, run_id=3):
                self.assertEqual(json.loads(formatter.format(record))["case_id"], 2)
            after_nested = json.loads(formatter.format(record))
            self.assertEqual(after_nested["case_id"], 1)
            self.assertNotIn("run_id", after_nested)
            self.assertNotIn("secret-never-log", formatter.format(record))
        self.assertNotIn("case_id", json.loads(formatter.format(record)))

    def test_async_tasks_do_not_share_principal_or_case_context(self):
        formatter = StructuredFormatter()
        logger = get_logger("test")

        async def worker(case_id):
            with LogContext(logger, case_id=case_id, user_name=f"lawyer-{case_id}"):
                await asyncio.sleep(0)
                return json.loads(formatter.format(logging.LogRecord("event", logging.INFO, "file", 1, "done", (), None)))

        async def run():
            return await asyncio.gather(worker(1), worker(2))

        first, second = asyncio.run(run())
        self.assertEqual((first["case_id"], first["user_name"]), (1, "lawyer-1"))
        self.assertEqual((second["case_id"], second["user_name"]), (2, "lawyer-2"))

    def test_exception_logs_only_type_and_escapes_multiline_message(self):
        error = RuntimeError("secret-case-body and credential")
        record = logging.LogRecord("event", logging.ERROR, "file", 1, "failed\nforged line", (), (RuntimeError, error, None))
        output = StructuredFormatter().format(record)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertNotIn("secret-case-body", output)
        self.assertEqual(json.loads(output)["error_type"], "RuntimeError")

    def test_common_credential_formats_are_redacted(self):
        record = logging.LogRecord("event", logging.INFO, "file", 1, "Bearer ABCDE token=FGHI password:JKLM api_key=NOPQ", (), None)
        output = StructuredFormatter().format(record)
        for secret in ("ABCDE", "FGHI", "JKLM", "NOPQ"):
            self.assertNotIn(secret, output)
        self.assertIn("[REDACTED]", output)


if __name__ == "__main__":
    unittest.main()
