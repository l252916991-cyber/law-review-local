"""E06 source kinds and exact boundaries over isolated synthetic data."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.db import connect, init_db, transaction
from app.forge_bridge import run_framework
from app.forge_readonly import ReadonlyCaseSession
from app.forge_sources import statute_source, verify_statutes
from app.host_ports import capture_case_rows
from tests.support import IsolatedDatabaseTestCase
from tests.test_forge_readonly import dump_source, prepare, request, write_corpus


def source():
    return statute_source({"document_id": "synthetic-law", "law_name": "合成法",
                           "version_date": "2020-01-01", "article_number": 1,
                           "text": "第一条 合成测试原文。", "source_url": "https://example.invalid/law"},
                          "a" * 64, 1)


class StatuteIntegrityTests(unittest.TestCase):
    def test_exact_statute_source_has_no_invented_case_page(self):
        record = source()
        self.assertNotIn("page_no", record)
        self.assertEqual(record["text_sha256"], hashlib.sha256(record["text"].encode()).hexdigest())
        valid, _ = verify_statutes("[statute:1]「合成测试原文」", [record], corpus_version="a"*64)
        self.assertTrue(valid)

    def test_unquoted_unknown_empty_or_invented_statute_quotes_fail(self):
        for text in ("[statute:1]", "[statute:2]「合成测试原文」", "[statute:1]「」",
                     "[statute:1]「模型编造」", "[statute:1]「合成测试原文」[statute:1]"):
            with self.subTest(text=text):
                self.assertFalse(verify_statutes(text, [source()])[0])

    def test_mutated_text_identity_duplicate_and_foreign_corpus_fail(self):
        record = source()
        for key, changed in (("text", "篡改原文"), ("kind", "case_page"),
                             ("article_number", 0), ("version_date", ""),
                             ("document_id", ""), ("corpus_version", "b"*64)):
            bad = deepcopy(record); bad[key] = changed
            with self.subTest(key=key):
                self.assertFalse(verify_statutes("[statute:1]「合成测试原文」", [bad],
                                                corpus_version="a"*64)[0])
        self.assertFalse(verify_statutes("[statute:1]「合成测试原文」", [record, record])[0])

    def test_statute_text_bounds_are_exact(self):
        record = source()
        hit = {**record, "text": "字" * 20_000}
        self.assertEqual(len(statute_source(hit, "a"*64, 1)["text"]), 20_000)
        hit["text"] += "字"
        with self.assertRaisesRegex(ValueError, "bound"):
            statute_source(hit, "a"*64, 1)


class SourceBoundaryTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def test_snapshot_equal_limits_pass_and_one_less_fails(self):
        rows = capture_case_rows(1)
        count = len(rows["pages"])
        chars = sum(len(page["text"]) for page in rows["pages"])
        self.assertEqual(capture_case_rows(1, max_pages=count, max_chars=chars), rows)
        for bounds in ({"max_pages": count-1}, {"max_chars": chars-1}):
            with self.subTest(bounds=bounds), self.assertRaisesRegex(ValueError, "snapshot bounds"):
                capture_case_rows(1, **bounds)

    def test_page_equal_limit_passes_and_over_limit_fails(self):
        with transaction() as conn:
            page, original = conn.execute("SELECT id,text FROM pages ORDER BY id LIMIT 1").fetchone()
        def restore():
            with transaction() as conn:
                conn.execute("UPDATE pages SET text=? WHERE id=?", (original, page))
        self.addCleanup(restore)
        with transaction() as conn:
            conn.execute("UPDATE pages SET text=? WHERE id=?", ("字"*20_000, page))
        capture_case_rows(1)
        with transaction() as conn:
            conn.execute("UPDATE pages SET text=text || '字' WHERE id=?", (page,))
        with self.assertRaisesRegex(ValueError, "20000"):
            capture_case_rows(1)

    def test_pure_statute_projection_keeps_legacy_comparison_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            write_corpus(Path(folder), "本条仅为合成工程测试。")
            with patch.dict(os.environ, {"LAW_REVIEW_LEGAL_CORPUS_DIR": folder}):
                before = dump_source()
                with ReadonlyCaseSession(request(question="刑法第二百六十四条的法条")) as session:
                    prepare(session)
                    finished = session.call("finish")
                    self.assertEqual(finished["bundle"]["pages"], [])
                    self.assertEqual(finished["answer_projection"], "statute-source-v1")
                    self.assertTrue(finished["validation"]["valid"])
                    self.assertIn("[statute:1]", finished["strict_answer"])
                    self.assertFalse(finished["validation"]["semantic_entailment_checked"])
                    comparison = session.compare_native()
                    self.assertTrue(comparison["passed"])
                    self.assertEqual(comparison["answer_comparison_scope"],
                                     "legacy_critic_before_source_projection")
                self.assertEqual(before, dump_source())

    def test_retrieved_bundle_equal_character_limit_passes_and_over_limit_fails(self):
        with transaction() as conn:
            document_id, name, kind = conn.execute("SELECT id,name,doc_type FROM documents WHERE case_id=1 ORDER BY id LIMIT 1").fetchone()
            first = conn.execute("SELECT COALESCE(MAX(page_no),0) FROM pages WHERE document_id=?", (document_id,)).fetchone()[0]+1
        inserted = []
        def remove():
            with transaction() as conn:
                conn.executemany("DELETE FROM pages WHERE id=?", [(value,) for value in inserted])
        self.addCleanup(remove)
        with transaction() as conn:
            for offset in range(11):
                inserted.append(conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,?,?)",
                                             (document_id, first+offset, "界"*20_000)).lastrowid)
        with ReadonlyCaseSession(request()) as session:
            prepare(session)
            contexts = [{"document_id": document_id, "page_no": first+i,
                         "name": name, "doc_type": kind, "quote": "界"} for i in range(11)]
            with session._scope(session.snapshot):
                finished = session._finish(contexts[:10])
                self.assertEqual(sum(len(page["text"]) for page in finished["bundle"]["pages"]), 200_000)
                with self.assertRaisesRegex(ValueError, "bundle limit"):
                    session._finish(contexts)

    @unittest.skipUnless(os.getenv("LAW_REVIEW_FORGE_PYTHON"), "separate framework interpreter required")
    def test_large_raw_pages_do_not_get_silently_truncated_as_tool_json(self):
        with closing(connect()) as conn:
            originals = conn.execute("SELECT id,text FROM pages ORDER BY id LIMIT 6").fetchall()
        def restore():
            with transaction() as conn:
                conn.executemany("UPDATE pages SET text=? WHERE id=?", [(row[1], row[0]) for row in originals])
        self.addCleanup(restore)
        with transaction() as conn:
            conn.executemany("UPDATE pages SET text=? WHERE id=?",
                             [("固定回报审批记录。" + "合成材料" * 4900, row[0]) for row in originals])
        before = dump_source()
        with ReadonlyCaseSession(request()) as session:
            report = run_framework(session, Path(os.environ["LAW_REVIEW_FORGE_PYTHON"]))
            self.assertEqual(report["status"], "succeeded", report)
            raw_chars = sum(len(page["text"]) for page in session.outputs["finish"]["bundle"]["pages"])
            self.assertGreater(raw_chars, 50_000)
            self.assertLessEqual(raw_chars, 200_000)
            self.assertTrue(report["comparison"]["passed"])
        self.assertEqual(before, dump_source())

    @unittest.skipUnless(os.getenv("LAW_REVIEW_FORGE_PYTHON"), "separate framework interpreter required")
    def test_real_worker_accepts_verified_statutes_without_inventing_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            write_corpus(Path(folder), "仅作合成工程测试，需律师核验。")
            with patch.dict(os.environ, {"LAW_REVIEW_LEGAL_CORPUS_DIR": folder}):
                before = dump_source()
                with ReadonlyCaseSession(request(question="刑法第二百六十四条的法条")) as session:
                    report = run_framework(session, Path(os.environ["LAW_REVIEW_FORGE_PYTHON"]))
                    self.assertEqual(report["status"], "succeeded", report)
                    self.assertEqual(report["result"]["answer_projection"], "statute-source-v1")
                self.assertEqual(before, dump_source())
