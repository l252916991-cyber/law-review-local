import io
import os
import tempfile
import unittest
from contextlib import closing
from tests.support import IsolatedDatabaseTestCase
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch


TEST_DATA = tempfile.mkdtemp(prefix="lexvault-api-tests-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402


class APIContractTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        with closing(connect()) as conn, conn:
            conn.execute("DELETE FROM cases WHERE id <> 1")
            conn.commit()
        cls.client_context.__exit__(None, None, None)

    def test_model_dependency_errors_are_actionable_and_redacted(self):
        for path, target, payload in [
            ("vector-index", "app.rag.HybridRetriever.ensure_vector_index", None),
            ("evaluate-rag", "app.evaluation.evaluate_case", None),
            ("agent-compare", "app.rag.recall_memories", {"question": "固定回报是否存在矛盾？"}),
        ]:
            for code in ("embedding_model_unavailable", "rerank_model_unavailable"):
                with self.subTest(path=path, code=code), patch(target, side_effect=RuntimeError(code + ":SECRET_TOKEN")):
                    response = self.client.post(f"/api/cases/1/{path}", json=payload)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"]["code"], code)
                self.assertIn("模型", response.json()["detail"]["message"])
                self.assertNotIn("SECRET_TOKEN", response.text)

    def test_health_contract(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["private_mode"])

    def test_case_crud_validation_matrix(self):
        for payload, expected in [({}, 422), ({"title": "短"}, 422), ({"title": "A" * 121}, 422)]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/cases", json=payload).status_code, expected)
        response = self.client.post("/api/cases", json={"title": "隔离测试案件", "case_type": "民事"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["title"], "隔离测试案件")

    def test_missing_case_matrix(self):
        for method, path in [
            ("get", "/api/cases/999999"),
            ("get", "/api/cases/999999/documents"),
            ("get", "/api/cases/999999/evidence"),
            ("get", "/api/cases/999999/search?q=x"),
            ("get", "/api/cases/999999/export"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)(path).status_code, 404)

    def test_search_validation_matrix(self):
        for query in ("", "x" * 501):
            with self.subTest(length=len(query)):
                self.assertEqual(self.client.get("/api/cases/1/search", params={"q": query}).status_code, 422)
        self.assertEqual(self.client.get("/api/cases/1/search", params={"q": "固定回报"}).status_code, 200)

    def test_chat_validation_matrix(self):
        for question in ("", "x" * 3001):
            with self.subTest(length=len(question)):
                self.assertEqual(self.client.post("/api/cases/1/chat", json={"question": question}).status_code, 422)
        response = self.client.post(
            "/api/cases/1/chat",
            json={"question": "募集资金总额？", "use_llm": False, "use_remote_embeddings": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["citations"])

    def test_chat_remote_retrieval_failure_is_actionable_and_redacted(self):
        with patch(
            "app.rag.HybridRetriever.retrieve",
            side_effect=RuntimeError("embedding_model_unavailable:SECRET_TOKEN"),
        ):
            response = self.client.post(
                "/api/cases/1/chat", json={"question": "固定回报是否存在", "use_llm": False}
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "embedding_model_unavailable")
        self.assertNotIn("SECRET_TOKEN", response.text)

    def test_upload_text_and_sanitize_filename(self):
        response = self.client.post(
            "/api/cases/1/documents",
            files=[("files", ("../../恶意合同.txt", "甲乙于2026年1月1日签订合同。", "text/plain"))],
        )
        self.assertEqual(response.status_code, 201)
        document = response.json()["documents"][0]
        self.assertEqual(document["name"], "恶意合同.txt")
        self.assertNotIn("..", Path(document["stored_path"]).name)

    def test_upload_unsupported_file_is_reported_without_orphan(self):
        response = self.client.post(
            "/api/cases/1/documents", files=[("files", ("payload.exe", b"MZ", "application/octet-stream"))]
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["documents"], [])
        self.assertIn("暂不支持", response.json()["failures"][0]["error"])

    def test_upload_file_count_limit(self):
        files = [("files", (f"{index}.txt", "x", "text/plain")) for index in range(31)]
        self.assertEqual(self.client.post("/api/cases/1/documents", files=files).status_code, 400)

    def test_batch_upload_sanitizes_filename_before_queue(self):
        from app.db import get_db_path

        queued = AsyncMock(return_value="job-1")
        # This tests filename staging, not a real Redis connection. The full
        # offline suite intentionally blocks Redis during application startup.
        with patch("app.main.enqueue_batch_import", queued), patch.object(app.state, "redis_available", True):
            response = self.client.post(
                "/api/cases/1/batch-import",
                files=[("files", ("../../逃逸.txt", "批量内容", "text/plain"))],
            )
        self.assertEqual(response.status_code, 202)
        file_info = queued.await_args.args[2][0]
        self.assertEqual(file_info["filename"], "逃逸.txt")
        self.assertTrue(Path(file_info["stored_path"]).resolve().is_relative_to(get_db_path().parent.resolve()))

    def test_batch_status_not_found(self):
        self.assertEqual(self.client.get("/api/batch-imports/999999").status_code, 404)

    def _create_evidence(self, **overrides):
        payload = {
            "title": "综合测试证据", "category": "书证", "fact": "证明测试事实",
            "source_document_id": 1, "source_page_start": 1, "source_page_end": 3,
        }
        payload.update(overrides)
        return self.client.post("/api/cases/1/evidence", json=payload)

    def test_evidence_enum_validation_matrix(self):
        dimensions = {
            "category": ["证人证言", "", "书 证"],
            "credibility": ["最高", "", "未知"],
            "status": ["已删除", "", "完成"],
        }
        for field, values in dimensions.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(self._create_evidence(**{field: value}).status_code, 422)

    def test_evidence_page_range_validated_on_create(self):
        self.assertEqual(self._create_evidence(source_page_start=5, source_page_end=4).status_code, 400)

    def test_evidence_page_range_validated_on_partial_update(self):
        evidence_id = self._create_evidence().json()["evidence"]["id"]
        self.assertEqual(self.client.patch(f"/api/evidence/{evidence_id}", json={"source_page_start": 4}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/evidence/{evidence_id}", json={"source_page_end": 0}).status_code, 422)

    def test_evidence_cross_case_document_is_rejected(self):
        case_id = self.client.post("/api/cases", json={"title": "另一个案件"}).json()["id"]
        response = self.client.post(
            f"/api/cases/{case_id}/evidence",
            json={"title": "跨案件证据", "category": "书证", "fact": "越权", "source_document_id": 1},
        )
        self.assertEqual(response.status_code, 404)

    def test_evidence_update_empty_and_missing(self):
        evidence_id = self._create_evidence(source_document_id=None).json()["evidence"]["id"]
        self.assertEqual(self.client.patch(f"/api/evidence/{evidence_id}", json={}).status_code, 400)
        self.assertEqual(self.client.patch("/api/evidence/999999", json={"title": "不存在"}).status_code, 404)

    def test_evidence_delete_cascades_relations_and_annotations(self):
        first = self._create_evidence(source_document_id=None).json()["evidence"]["id"]
        second = self._create_evidence(title="第二项证据", source_document_id=None).json()["evidence"]["id"]
        with closing(connect()) as conn, conn:
            conn.execute("INSERT INTO evidence_relations(case_id,from_evidence_id,to_evidence_id) VALUES (1,?,?)", (first, second))
            conn.execute(
                "INSERT INTO evidence_annotations(evidence_id,user_name,annotation_type,content,created_at) VALUES (?,'测试','备注','x','now')",
                (first,),
            )
            conn.commit()
        payload = self.client.delete(f"/api/evidence/{first}").json()["deleted"]
        self.assertEqual(payload["relations_deleted"], 1)
        self.assertEqual(payload["annotations_deleted"], 1)

    def test_document_and_page_not_found(self):
        self.assertEqual(self.client.patch("/api/documents/999999/directory", json={"summary": "x"}).status_code, 404)
        self.assertEqual(self.client.get("/api/documents/999999/pages/1").status_code, 404)

    def test_export_is_valid_zip(self):
        response = self.client.get("/api/cases/1/export")
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn("案件摘要.md", archive.namelist())
            self.assertTrue(all(not name.startswith("/") and ".." not in Path(name).parts for name in archive.namelist()))

    def test_lawbench_endpoint_hides_answers(self):
        response = self.client.get("/api/benchmarks/lawbench", params={"limit": 200, "seed": 7})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["examples"]), 200)
        self.assertTrue(all("answer" not in item for item in response.json()["examples"]))


if __name__ == "__main__":
    unittest.main()
