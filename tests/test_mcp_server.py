"""MCP stdio surface: read-only tools, case bound at start, local access only."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.integration.mcp_server import PROTOCOL_VERSION, McpServer, main, serve
from app.storage.db import db_scope, init_db, now, transaction


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-mcp-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case_a = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", ("甲案", now(), now())
            ).lastrowid
            self.case_b = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", ("乙案", now(), now())
            ).lastrowid
            self.doc_a = self._document(conn, self.case_a, "甲合同")
            self.doc_b = self._document(conn, self.case_b, "乙合同")
        self.server = McpServer(self.case_a)

    @staticmethod
    def _document(conn, case_id: int, name: str) -> int:
        document_id = conn.execute(
            """INSERT INTO documents(case_id,name,mime_type,pages,status,created_at,updated_at)
               VALUES (?,?,'text/plain',1,'已索引',?,?)""",
            (case_id, name, now(), now()),
        ).lastrowid
        conn.execute(
            "INSERT INTO pages(document_id,page_no,text,summary) VALUES (?,?,?,?)",
            (document_id, 1, "正文", "摘要"),
        )
        return document_id

    def _call(self, method: str, params: dict | None = None, request_id: int = 1) -> dict:
        return self.server.handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

    def test_initialize_advertises_tools_capability(self):
        response = self._call("initialize")
        result = response["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "lexvault-case-tools")

    def test_tools_list_matches_the_readonly_allowlist(self):
        tools = self._call("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(len(tools), 9)
        self.assertIn("get_page", names)
        self.assertIn("verify_statute", names)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_tool_call_returns_serialised_result(self):
        response = self._call("tools/call", {"name": "case_overview", "arguments": {}})
        content = response["result"]["content"][0]
        self.assertEqual(content["type"], "text")
        payload = json.loads(content["text"])
        self.assertEqual(payload["items"][0]["title"], "甲案")
        self.assertEqual(payload["case_id"], self.case_a)

    def test_tool_call_cannot_reach_another_case(self):
        response = self._call(
            "tools/call", {"name": "get_page", "arguments": {"document_id": self.doc_b, "page_no": 1}}
        )
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["error"], "tool_failed")

    def test_unknown_tool_is_rejected_without_execution(self):
        response = self._call("tools/call", {"name": "delete_evidence", "arguments": {}})
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["error"], "unsupported_tool")

    def test_unknown_method_is_a_protocol_error(self):
        response = self._call("resources/list")
        self.assertEqual(response["error"]["code"], -32601)

    def test_notifications_get_no_response(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_serve_round_trip_writes_one_response_per_request(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
            + "not-json\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        )
        stdout = io.StringIO()
        serve(self.server, stdin, stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["result"], {})
        self.assertEqual(lines[1]["error"]["code"], -32700)
        self.assertEqual(len(lines[2]["result"]["tools"]), 9)

    def test_token_mode_refuses_to_start(self):
        with patch.dict(os.environ, {"LAW_REVIEW_AUTH_MODE": "token"}):
            self.assertEqual(main(["--case-id", str(self.case_a)]), 2)

    def test_case_binding_is_validated(self):
        with self.assertRaises(ValueError):
            McpServer(0)


if __name__ == "__main__":
    unittest.main()
