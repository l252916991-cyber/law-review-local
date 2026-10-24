"""Tool catalog: server-owned scope, bounded payloads and a fail-closed statute tool."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent_tools import (
    MAX_TOOL_TEXT_CHARS,
    READONLY_TOOL_NAMES,
    TOOL_SCHEMAS,
    execute_readonly_tool,
)
from app.db import db_scope, init_db, now, transaction
from app.security import Principal, _principal


class ToolCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-tools-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        marker = _principal.set(
            Principal("本机律师", True, permissions=frozenset({"view", "edit", "approve", "export", "manage"}))
        )
        self.addCleanup(_principal.reset, marker)
        with transaction() as conn:
            self.case_a = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", ("甲案", now(), now())
            ).lastrowid
            self.case_b = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", ("乙案", now(), now())
            ).lastrowid
            self.doc_a = self._document(conn, self.case_a, "甲合同", ["第一页正文", "第二页正文", "第三页正文"])
            self.doc_b = self._document(conn, self.case_b, "乙合同", ["乙案机密正文"])
            conn.execute(
                "INSERT INTO evidence(case_id,title,category,fact,created_at) VALUES (?,?,?,?,?)",
                (self.case_a, "证据一", "书证", "事实", now()),
            )

    @staticmethod
    def _document(conn, case_id: int, name: str, pages: list[str]) -> int:
        document_id = conn.execute(
            """INSERT INTO documents(case_id,name,mime_type,pages,status,created_at,updated_at)
               VALUES (?,?,'text/plain',?,'已索引',?,?)""",
            (case_id, name, len(pages), now(), now()),
        ).lastrowid
        for page_no, text in enumerate(pages, 1):
            conn.execute(
                "INSERT INTO pages(document_id,page_no,text,summary) VALUES (?,?,?,?)",
                (document_id, page_no, text, text[:40]),
            )
        return document_id

    def test_get_page_returns_the_requested_page_with_neighbours(self):
        result = execute_readonly_tool(self.case_a, "get_page", {"document_id": self.doc_a, "page_no": 2, "radius": 1})
        self.assertEqual([item["page_no"] for item in result["items"]], [1, 2, 3])
        self.assertEqual(result["items"][1]["text"], "第二页正文")
        self.assertFalse(any(item["truncated"] for item in result["items"]))
        self.assertEqual(result["items"][1]["document_name"], "甲合同")

    def test_get_page_refuses_a_document_from_another_case(self):
        with self.assertRaises(ValueError):
            execute_readonly_tool(self.case_a, "get_page", {"document_id": self.doc_b, "page_no": 1})

    def test_get_page_requires_a_real_page_number(self):
        for params in (
            {"document_id": self.doc_a},
            {"document_id": self.doc_a, "page_no": 0},
            {"document_id": self.doc_a, "page_no": "2"},
            {"document_id": self.doc_a, "page_no": 1, "radius": 3},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                execute_readonly_tool(self.case_a, "get_page", params)

    def test_get_page_bounds_the_total_payload(self):
        with transaction() as conn:
            document_id = self._document(conn, self.case_a, "长文书", ["长" * (MAX_TOOL_TEXT_CHARS + 500)])
        result = execute_readonly_tool(self.case_a, "get_page", {"document_id": document_id, "page_no": 1})
        self.assertEqual(len(result["items"][0]["text"]), MAX_TOOL_TEXT_CHARS)
        self.assertTrue(result["items"][0]["truncated"])

    def test_document_outline_lists_pages_without_the_body(self):
        result = execute_readonly_tool(self.case_a, "document_outline", {"document_id": self.doc_a})
        self.assertEqual([item["page_no"] for item in result["items"]], [1, 2, 3])
        self.assertNotIn("正文", str(result["items"][0].get("text", "")))
        self.assertEqual(result["tool_metrics"]["pages"], 3)

    def test_case_overview_counts_only_its_own_case(self):
        result = execute_readonly_tool(self.case_a, "case_overview", {})
        overview = result["items"][0]
        self.assertEqual(overview["documents"], 1)
        self.assertEqual(overview["pages"], 3)
        self.assertEqual(overview["evidence"], 1)
        self.assertEqual(overview["title"], "甲案")

    def test_verify_statute_is_fail_closed_without_a_corpus(self):
        with patch.dict(os.environ, {"LAW_REVIEW_LEGAL_CORPUS_DIR": ""}):
            result = execute_readonly_tool(
                self.case_a, "verify_statute", {"law_name": "中华人民共和国民法典", "article_number": 585}
            )
        self.assertEqual(result["tool_metrics"]["status"], "needs_review")
        self.assertEqual(result["items"], [])

    def test_verify_statute_maps_status_and_never_returns_ok_from_text_search(self):
        canned = {
            "status": "ok",
            "mode": "citation",
            "summary": "精确核验到 1 条法条",
            "hits": [{"law_name": "中华人民共和国民法典", "article_number": 585, "text": "正文", "version_date": "2020-05-28"}],
            "warning": None,
        }
        with patch("app.statutory_retrieval.verify_citation", return_value=canned):
            result = execute_readonly_tool(
                self.case_a, "verify_statute", {"law_name": "中华人民共和国民法典", "article_number": 585}
            )
        self.assertEqual(result["tool_metrics"]["status"], "ok")
        self.assertEqual(result["items"][0]["status"], "ok")

    def test_verify_statute_rejects_bad_arguments(self):
        for params in (
            {"law_name": "", "article_number": 1},
            {"law_name": "民法典", "article_number": 0},
            {"law_name": "民法典", "article_number": "585"},
            {"law_name": "民法典", "article_number": 1, "version_date": "2020/05/28"},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                execute_readonly_tool(self.case_a, "verify_statute", params)

    def test_search_statutes_only_returns_candidates(self):
        canned = {
            "status": "candidate",
            "mode": "discovery",
            "summary": "检索到 1 条",
            "hits": [{"law_name": "民法典", "article_number": 585, "text": "正文"}],
            "warning": None,
        }
        with patch("app.statutory_retrieval.search_statute_text", return_value=canned):
            result = execute_readonly_tool(self.case_a, "search_statutes", {"query": "违约金"})
        self.assertEqual(result["items"][0]["status"], "candidate")
        self.assertNotEqual(result["tool_metrics"]["status"], "ok")

    def test_schema_registry_covers_every_readonly_tool(self):
        self.assertEqual(set(TOOL_SCHEMAS), set(READONLY_TOOL_NAMES))
        for name, schema in TOOL_SCHEMAS.items():
            with self.subTest(tool=name):
                function = schema["function"]
                self.assertEqual(function["name"], name)
                self.assertTrue(function["description"])
                self.assertFalse(function["parameters"]["additionalProperties"])
                self.assertTrue(function["description"].strip())

    def test_unknown_parameters_are_reported_per_tool(self):
        with self.assertRaises(ValueError):
            execute_readonly_tool(self.case_a, "case_overview", {"case_id": self.case_b})
        with self.assertRaises(ValueError):
            execute_readonly_tool(self.case_a, "document_outline", {"document_id": self.doc_a, "depth": 2})


if __name__ == "__main__":
    unittest.main()
