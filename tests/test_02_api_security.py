"""
扩展API层测试 - 边界测试、错误处理、验证
"""

import io
import os
import tempfile
import unittest
from contextlib import closing
from tests.support import IsolatedDatabaseTestCase
from pathlib import Path
from unittest.mock import AsyncMock, patch

TEST_DATA = tempfile.mkdtemp(prefix="lexvault-api-tests-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect, init_db, now  # noqa: E402
from app.main import app  # noqa: E402


class APIBoundaryTest(IsolatedDatabaseTestCase):
    """API边界值和输入验证测试"""

    @classmethod
    def setUpClass(cls):
        init_db(seed=False)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        # 创建测试案件
        resp = cls.client.post("/api/cases", json={"title": "API测试案件", "case_type": "民事"})
        cls.case_id = resp.json()["id"]

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_request_correlation_id_echoed_generated_and_logged(self):
        import json as json_module
        import logging
        from app.logger import StructuredFormatter, request_id as request_id_var
        echoed = self.client.get("/api/health", headers={"X-Request-ID": "req-fixed-123456"})
        self.assertEqual(echoed.headers["x-request-id"], "req-fixed-123456")
        generated = self.client.get("/api/health")
        fresh = generated.headers["x-request-id"]
        self.assertTrue(8 <= len(fresh) <= 80)
        self.assertNotEqual(fresh, "req-fixed-123456")
        # Hostile or malformed ids are replaced, not echoed.
        hostile = self.client.get("/api/health", headers={"X-Request-ID": "x" * 200 + " evil\n"})
        self.assertNotIn("evil", hostile.headers["x-request-id"])
        records: list[logging.LogRecord] = []
        rendered_lines: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)
                rendered_lines.append(StructuredFormatter().format(record))

        logger = logging.getLogger("law_review.access")
        capture = Capture()
        logger.addHandler(capture)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            self.client.get("/api/health", headers={"X-Request-ID": "req-logged-123456"})
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)
        self.assertTrue(records)
        self.assertEqual(getattr(records[-1], "status", None), 200)
        self.assertIsInstance(getattr(records[-1], "duration_ms", None), int)
        # The live access line itself carries the correlation id: the middleware
        # must log before resetting the context, or operators lose the linkage.
        token = request_id_var.set("req-logged-123456")
        try:
            rendered = json_module.loads(StructuredFormatter().format(records[-1]))
        finally:
            request_id_var.reset(token)
        self.assertEqual(rendered["request_id"], "req-logged-123456")
        self.assertTrue(any("req-logged-123456" in line for line in rendered_lines),
                        f"access line lost the request id: {rendered_lines}")

    def test_case_title_min_length_boundary(self):
        """案件标题最小长度边界"""
        # 1字符应该失败
        resp = self.client.post("/api/cases", json={"title": "短"})
        self.assertEqual(resp.status_code, 422)

        # 2字符应该成功
        resp = self.client.post("/api/cases", json={"title": "正常"})
        self.assertEqual(resp.status_code, 201)

    def test_case_title_max_length_boundary(self):
        """案件标题最大长度边界"""
        # 120字符应该成功
        title_120 = "标" * 120
        resp = self.client.post("/api/cases", json={"title": title_120})
        self.assertEqual(resp.status_code, 201)

        # 121字符应该失败
        title_121 = "标" * 121
        resp = self.client.post("/api/cases", json={"title": title_121})
        self.assertEqual(resp.status_code, 422)

    def test_case_type_enum_validation(self):
        """案件类型枚举验证"""
        valid_types = ["民事", "刑事", "行政"]
        for case_type in valid_types:
            resp = self.client.post("/api/cases", json={"title": f"测试{case_type}", "case_type": case_type})
            self.assertEqual(resp.status_code, 201, f"{case_type}应该有效")

    def test_search_query_min_length_boundary(self):
        """搜索查询最小长度边界"""
        # 空查询应该失败
        resp = self.client.get(f"/api/cases/{self.case_id}/search", params={"q": ""})
        self.assertEqual(resp.status_code, 422)

        # 1字符应该成功
        resp = self.client.get(f"/api/cases/{self.case_id}/search", params={"q": "测"})
        self.assertEqual(resp.status_code, 200)

    def test_search_query_max_length_boundary(self):
        """搜索查询最大长度边界"""
        # 500字符应该成功
        query_500 = "搜" * 500
        resp = self.client.get(f"/api/cases/{self.case_id}/search", params={"q": query_500})
        self.assertEqual(resp.status_code, 200)

        # 501字符应该失败
        query_501 = "搜" * 501
        resp = self.client.get(f"/api/cases/{self.case_id}/search", params={"q": query_501})
        self.assertEqual(resp.status_code, 422)

    def test_chat_question_length_boundaries(self):
        """问答问题长度边界"""
        # 空问题失败
        resp = self.client.post(f"/api/cases/{self.case_id}/chat", json={"question": ""})
        self.assertEqual(resp.status_code, 422)

        # 3000字符成功
        question_3000 = "问" * 3000
        resp = self.client.post(
            f"/api/cases/{self.case_id}/chat",
            json={"question": question_3000, "use_llm": False, "use_remote_embeddings": False},
        )
        self.assertEqual(resp.status_code, 200)

        # 3001字符失败
        question_3001 = "问" * 3001
        resp = self.client.post(f"/api/cases/{self.case_id}/chat", json={"question": question_3001, "use_llm": False})
        self.assertEqual(resp.status_code, 422)

    def test_file_upload_count_limit(self):
        """文件上传数量限制"""
        # 30个文件应该成功
        files_30 = [("files", (f"file{i}.txt", "content", "text/plain")) for i in range(30)]
        resp = self.client.post(f"/api/cases/{self.case_id}/documents", files=files_30)
        self.assertEqual(resp.status_code, 201)

        # 31个文件应该失败
        files_31 = [("files", (f"file{i}.txt", "content", "text/plain")) for i in range(31)]
        resp = self.client.post(f"/api/cases/{self.case_id}/documents", files=files_31)
        self.assertEqual(resp.status_code, 400)

    def test_evidence_page_range_validation(self):
        """证据页码范围验证"""
        # 创建测试文档
        resp = self.client.post(
            f"/api/cases/{self.case_id}/documents",
            files=[("files", ("test.txt", "测试内容", "text/plain"))]
        )
        doc_id = resp.json()["documents"][0]["id"]

        # 结束页 < 起始页应该失败
        evidence_data = {
            "title": "测试证据",
            "category": "书证",
            "fact": "测试事实",
            "source_document_id": doc_id,
            "source_page_start": 10,
            "source_page_end": 5
        }
        resp = self.client.post(f"/api/cases/{self.case_id}/evidence", json=evidence_data)
        self.assertEqual(resp.status_code, 400)

        # 结束页 >= 起始页应该成功
        evidence_data["source_page_start"] = 1
        evidence_data["source_page_end"] = 1
        resp = self.client.post(f"/api/cases/{self.case_id}/evidence", json=evidence_data)
        self.assertEqual(resp.status_code, 201)

    def test_evidence_category_enum_validation(self):
        """证据类别枚举验证"""
        valid_categories = ['书证', '物证', '言词证据', '银行流水', '审计报告', '询问笔录', '电子数据', '其他材料']

        resp = self.client.post(
            f"/api/cases/{self.case_id}/documents",
            files=[("files", ("test.txt", "内容", "text/plain"))]
        )
        doc_id = resp.json()["documents"][0]["id"]

        for category in valid_categories:
            evidence_data = {
                "title": f"测试{category}",
                "category": category,
                "fact": "测试事实",
                "source_document_id": doc_id
            }
            resp = self.client.post(f"/api/cases/{self.case_id}/evidence", json=evidence_data)
            self.assertEqual(resp.status_code, 201, f"{category}应该有效")

        # 无效类别
        evidence_data = {
            "title": "无效证据",
            "category": "无效类别",
            "fact": "测试"
        }
        resp = self.client.post(f"/api/cases/{self.case_id}/evidence", json=evidence_data)
        self.assertEqual(resp.status_code, 422)


class APIErrorHandlingTest(IsolatedDatabaseTestCase):
    """API错误处理测试"""

    @classmethod
    def setUpClass(cls):
        init_db(seed=False)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_nonexistent_case_returns_404(self):
        """不存在的案件返回404"""
        endpoints = [
            "/api/cases/99999",
            "/api/cases/99999/documents",
            "/api/cases/99999/evidence",
            "/api/cases/99999/search?q=test",
            "/api/cases/99999/chat",
        ]

        for endpoint in endpoints:
            if "search" in endpoint:
                resp = self.client.get(endpoint)
            elif "chat" in endpoint:
                resp = self.client.post(endpoint, json={"question": "test", "use_llm": False})
            else:
                resp = self.client.get(endpoint)

            self.assertEqual(resp.status_code, 404, f"{endpoint} 应该返回404")

    def test_nonexistent_document_returns_404(self):
        """不存在的文档返回404"""
        resp = self.client.get("/api/documents/99999")
        self.assertEqual(resp.status_code, 404)

        resp = self.client.get("/api/documents/99999/pages/1")
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_evidence_returns_404(self):
        """不存在的证据返回404"""
        resp = self.client.get("/api/evidence/99999")
        self.assertEqual(resp.status_code, 404)

        resp = self.client.delete("/api/evidence/99999")
        self.assertEqual(resp.status_code, 404)

    def test_malformed_json_returns_422(self):
        """格式错误的JSON返回422"""
        resp = self.client.post(
            "/api/cases",
            content="invalid json{",
            headers={"Content-Type": "application/json"}
        )
        self.assertEqual(resp.status_code, 422)

    def test_missing_required_fields_returns_422(self):
        """缺少必填字段返回422"""
        # 案件缺少title
        resp = self.client.post("/api/cases", json={})
        self.assertEqual(resp.status_code, 422)

        # 证据缺少必填字段
        resp = self.client.post("/api/cases/1/evidence", json={"title": "测试"})
        self.assertEqual(resp.status_code, 422)

    def test_invalid_query_parameters(self):
        """无效的查询参数"""
        # 创建测试案件
        resp = self.client.post("/api/cases", json={"title": "参数测试案件"})
        case_id = resp.json()["id"]

        # 缺少q参数
        resp = self.client.get(f"/api/cases/{case_id}/search")
        self.assertEqual(resp.status_code, 422)

    def test_unsupported_file_type_graceful_failure(self):
        """不支持的文件类型优雅失败"""
        resp = self.client.post("/api/cases", json={"title": "文件测试案件"})
        case_id = resp.json()["id"]

        # 上传不支持的文件
        resp = self.client.post(
            f"/api/cases/{case_id}/documents",
            files=[("files", ("malicious.exe", b"MZ", "application/octet-stream"))]
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["documents"], [])
        self.assertGreater(len(resp.json()["failures"]), 0)

    def test_empty_file_upload_handling(self):
        """空文件上传处理"""
        resp = self.client.post("/api/cases", json={"title": "空文件测试"})
        case_id = resp.json()["id"]

        resp = self.client.post(
            f"/api/cases/{case_id}/documents",
            files=[("files", ("empty.txt", b"", "text/plain"))]
        )
        self.assertEqual(resp.status_code, 201)

    def test_duplicate_evidence_allowed(self):
        """允许重复证据（业务规则）"""
        resp = self.client.post("/api/cases", json={"title": "重复证据测试"})
        case_id = resp.json()["id"]

        evidence_data = {
            "title": "重复证据",
            "category": "书证",
            "fact": "重复事实"
        }

        # 第一次创建
        resp1 = self.client.post(f"/api/cases/{case_id}/evidence", json=evidence_data)
        self.assertEqual(resp1.status_code, 201)

        # 第二次创建（应该也成功）
        resp2 = self.client.post(f"/api/cases/{case_id}/evidence", json=evidence_data)
        self.assertEqual(resp2.status_code, 201)
        self.assertNotEqual(resp1.json()["evidence"]["id"], resp2.json()["evidence"]["id"])


class APISecurityTest(IsolatedDatabaseTestCase):
    """API安全测试"""

    @classmethod
    def setUpClass(cls):
        init_db(seed=False)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        resp = cls.client.post("/api/cases", json={"title": "安全测试案件"})
        cls.case_id = resp.json()["id"]

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_untrusted_title_is_returned_as_json_not_executable_html(self):
        """Storage preserves text; HTML escaping belongs to the UI renderer."""
        xss_payloads = [
            "<script>alert('xss')</script>测试",
            "<img src=x onerror=alert('xss')>",
            "'; DROP TABLE cases; --",
        ]

        for payload in xss_payloads:
            resp = self.client.post("/api/cases", json={"title": payload})
            if resp.status_code == 201:
                case_data = resp.json()
                self.assertEqual(case_data["title"], payload)
                self.assertTrue(resp.headers["content-type"].startswith("application/json"))
                self.assertEqual(resp.headers["x-content-type-options"], "nosniff")

    def test_sql_injection_in_search_prevented(self):
        """搜索中的SQL注入被阻止"""
        sql_payloads = [
            "' OR '1'='1",
            "1' UNION SELECT * FROM cases--",
            "'; DROP TABLE documents; --",
        ]

        for payload in sql_payloads:
            resp = self.client.get(f"/api/cases/{self.case_id}/search", params={"q": payload})
            # 应该正常返回（参数化查询防止注入）
            self.assertEqual(resp.status_code, 200)
            # 不应该返回所有数据
            results = resp.json()
            self.assertIsInstance(results, list)
            with closing(connect()) as conn, conn:
                self.assertIsNotNone(conn.execute("SELECT id FROM cases WHERE id=?", (self.case_id,)).fetchone())
                for result in results:
                    owner = conn.execute("SELECT case_id FROM documents WHERE id=?", (result["document_id"],)).fetchone()
                    self.assertEqual(owner[0], self.case_id)

    def test_path_traversal_in_filename_prevented(self):
        """文件名中的路径穿越被阻止"""
        dangerous_filenames = [
            "../../etc/passwd",
            "..\\..\\windows\\system32\\config\\sam",
            "../../../root/.ssh/id_rsa",
        ]

        for filename in dangerous_filenames:
            resp = self.client.post(
                f"/api/cases/{self.case_id}/documents",
                files=[("files", (filename, "content", "text/plain"))]
            )
            self.assertEqual(resp.status_code, 201)
            if resp.json()["documents"]:
                stored_name = resp.json()["documents"][0]["name"]
                # 文件名应该被清理
                self.assertNotIn("..", stored_name)
                self.assertNotIn("/", stored_name)
                self.assertNotIn("\\", stored_name)

    def test_large_payload_handling(self):
        """超大payload处理"""
        # 超大标题
        large_title = "标" * 10000
        resp = self.client.post("/api/cases", json={"title": large_title})
        self.assertEqual(resp.status_code, 422)

    def test_special_characters_in_inputs(self):
        """输入中的特殊字符处理"""
        special_cases = {
            "title": "测试\x00案件",  # null字节
            "case_no": "案号\r\n换行",  # 换行符
            "description": "描述\t制表符",  # 制表符
        }

        resp = self.client.post("/api/cases", json=special_cases)
        if resp.status_code == 201:
            # 特殊字符应该被适当处理
            self.assertIsNotNone(resp.json()["id"])


if __name__ == "__main__":
    unittest.main()
