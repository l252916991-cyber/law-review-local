"""Declared response models document the core endpoints without reshaping them."""
from __future__ import annotations

from contextlib import closing

from fastapi.testclient import TestClient

from tests.support import IsolatedDatabaseTestCase

TYPED = {
    ("/api/cases", "get"): "CaseSummary",
    ("/api/cases/lifecycle/{lifecycle_status}", "get"): "CaseRow",
    ("/api/cases/{case_id}", "get"): "CaseDetail",
    ("/api/cases/{case_id}/documents", "get"): "DocumentSummary",
    ("/api/documents/{document_id}/pages/{page_no}", "get"): "PageView",
    ("/api/cases/{case_id}/search", "get"): "SearchHit",
    ("/api/cases/{case_id}/evidence", "get"): "EvidenceList",
    ("/api/cases/{case_id}/conversations", "get"): "ConversationSummary",
    ("/api/conversations/{conversation_id}/messages", "get"): "Message",
    ("/api/cases/{case_id}/chat", "post"): "ChatResult",
    ("/api/agent-runs/{run_id}", "get"): "AgentRunTrace",
}


def _success_schema(operation: dict) -> dict:
    responses = operation["responses"]
    body = responses.get("200") or responses.get("201")
    return body["content"]["application/json"]["schema"]


class ResponseSchemaTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app
        from app.storage.db import init_db

        init_db(seed=True)
        cls.app = app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_core_endpoints_publish_a_named_response_schema(self):
        spec = self.app.openapi()
        for (path, method), model in TYPED.items():
            with self.subTest(path=path):
                schema = _success_schema(spec["paths"][path][method])
                reference = schema.get("$ref") or schema.get("items", {}).get("$ref")
                self.assertEqual(reference, f"#/components/schemas/{model}")
                # Every model passes undeclared keys through instead of dropping them.
                self.assertIs(spec["components"]["schemas"][model].get("additionalProperties"), True)

    def test_list_responses_keep_every_selected_column(self):
        from app.storage.db import connect

        case_id = self.client.get("/api/cases").json()[0]["id"]
        documents = self.client.get(f"/api/cases/{case_id}/documents").json()
        with closing(connect()) as conn:
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(documents)")]
        self.assertTrue(documents)
        # Every column except the server-side storage path, which stays private.
        for document in documents:
            self.assertEqual(set(document), set(columns) - {"stored_path"})

    def test_undeclared_keys_survive_the_response_model(self):
        case_id = self.client.get("/api/cases").json()[0]["id"]
        payload = {"question": "宣传材料由谁审批？", "use_llm": False, "use_remote_embeddings": False,
                   "request_id": "schema-replay-0001"}
        first = self.client.post(f"/api/cases/{case_id}/chat", json=payload)
        replay = self.client.post(f"/api/cases/{case_id}/chat", json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        # The replay flag is not part of ChatResult; it must still be returned.
        self.assertIs(replay.json()["request_id_replayed"], True)
        self.assertEqual(replay.json()["message_id"], first.json()["message_id"])
