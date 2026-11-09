"""Native tool loop: read-only, bounded, and explicit about unsupported endpoints."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from app.review import tool_loop
from app.review.tool_loop import MAX_ROUNDS_LIMIT, ToolsUnsupportedError, run_tool_loop
from app.storage.db import db_scope, init_db, now, transaction


class _FakeResponse:
    def __init__(self, payload: dict):
        self.buffer = io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.closed = False

    def read1(self, size: int) -> bytes:
        return self.buffer.read(size)

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False


class _ScriptedOpener:
    """Returns queued payloads in order; a queued exception is raised instead."""

    def __init__(self, *queue):
        self.queue = list(queue)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(json.loads(request.data.decode("utf-8")))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


def _tool_call(name: str, arguments: str | dict, call_id: str = "call-1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
        },
    }


def _message(content: str = "", tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 4}}


class ToolLoopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-loop-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case_id = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", ("工具循环", now(), now())
            ).lastrowid

    def _run(self, opener, **kwargs):
        kwargs.setdefault("case_id", self.case_id)
        kwargs.setdefault("question", "问题")
        kwargs.setdefault("route", "事实检索")
        kwargs.setdefault("contexts", [{"name": "卷宗", "page_no": 1, "quote": "原文", "document_id": 1}])
        with patch.object(tool_loop, "local_llm_available", return_value=(True, "test-model")), \
                patch.object(tool_loop, "egress_opener", return_value=opener):
            return run_tool_loop(**kwargs)

    def test_tool_result_is_fed_back_and_the_second_round_answers(self):
        opener = _ScriptedOpener(
            _message(tool_calls=[_tool_call("documents", {"limit": 5})]),
            _message("根据材料，事实如下 [资料1]。\n\n请律师复核原文与上述结论。"),
        )
        result = self._run(opener)
        self.assertIn("请律师复核", result["answer"])
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual([item["tool"] for item in result["tool_trace"]], ["documents"])
        self.assertTrue(result["tool_trace"][0]["ok"])
        # The tool message must be present in the second request.
        roles = [message["role"] for message in opener.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(opener.requests[0]["tools"][0]["type"], "function")

    def test_unknown_tool_is_never_executed(self):
        opener = _ScriptedOpener(
            _message(tool_calls=[_tool_call("delete_evidence", {"id": 1})]),
            _message("无法执行该操作。\n\n请律师复核原文与上述结论。"),
        )
        result = self._run(opener)
        tool_message = opener.requests[1]["messages"][-1]
        payload = json.loads(tool_message["content"])
        self.assertEqual(payload["error"], "unsupported_tool")
        self.assertIn("documents", payload["allowed"])
        self.assertFalse(result["tool_trace"][0]["ok"])

    def test_invalid_json_arguments_are_reported_not_raised(self):
        opener = _ScriptedOpener(
            _message(tool_calls=[_tool_call("get_page", "{not json")]),
            _message("参数无效。\n\n请律师复核原文与上述结论。"),
        )
        self._run(opener)
        payload = json.loads(opener.requests[1]["messages"][-1]["content"])
        self.assertEqual(payload["error"], "invalid_arguments")

    def test_tool_call_budget_is_enforced(self):
        opener = _ScriptedOpener(
            _message(tool_calls=[_tool_call("documents", {}, "c1"), _tool_call("evidence", {}, "c2")]),
            _message("已完成。\n\n请律师复核原文与上述结论。"),
        )
        result = self._run(opener, max_tool_calls=1)
        self.assertEqual(result["tool_calls"], 1)
        tool_messages = [m for m in opener.requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual(json.loads(tool_messages[1]["content"])["error"], "tool_budget_exhausted")

    def test_round_budget_fails_instead_of_looping_forever(self):
        opener = _ScriptedOpener(
            _message(tool_calls=[_tool_call("documents", {}, "a")]),
            _message(tool_calls=[_tool_call("documents", {}, "b")]),
        )
        with self.assertRaises(RuntimeError):
            self._run(opener, max_rounds=2, max_tool_calls=5)

    def test_endpoint_rejecting_tools_is_reported_explicitly(self):
        opener = _ScriptedOpener(urllib.error.HTTPError("http://x", 400, "bad request", None,
                                  io.BytesIO(b'{"error":{"message":"tools are not supported"}}')))
        with self.assertRaises(ToolsUnsupportedError):
            self._run(opener)

    def test_memory_guard_400_is_a_failed_call_not_unsupported_tools(self):
        raw = b'{"error":{"code":"prefill_memory_exceeded","message":"memory guard rejected prompt"}}'
        opener = _ScriptedOpener(urllib.error.HTTPError("http://x", 400, "bad request", None, io.BytesIO(raw)))
        with self.assertRaises(RuntimeError) as caught:
            self._run(opener)
        self.assertNotIsInstance(caught.exception, ToolsUnsupportedError)
        self.assertEqual(caught.exception.__cause__.read(), raw)
        with transaction() as conn:
            row = conn.execute("SELECT kind,status,input_tokens FROM llm_calls ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row["kind"], row["status"], row["input_tokens"]), ("tool_loop", "failed", None))

    def test_tool_loop_refuses_a_non_openai_protocol(self):
        # The loop builds openai-completions payloads and reads tool_calls from
        # them; under another wire api it declines instead of misparsing, which
        # is what lets chat() fall back to a single-shot call.
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_API": "anthropic-messages"}):
            with self.assertRaises(ToolsUnsupportedError):
                self._run(_ScriptedOpener())

    def test_limits_are_validated(self):
        for kwargs in ({"max_rounds": 0}, {"max_rounds": MAX_ROUNDS_LIMIT + 1}, {"max_tool_calls": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self._run(_ScriptedOpener(), **kwargs)

    def test_oversized_tool_result_is_replaced_by_a_marker(self):
        opener = _ScriptedOpener(
            _message(tool_calls=[_tool_call("documents", {})]),
            _message("ok\n\n请律师复核原文与上述结论。"),
        )
        huge = {"count": 1, "items": [{"text": "x" * (tool_loop.MAX_TOOL_RESULT_CHARS + 100)}]}
        with patch.object(tool_loop, "execute_readonly_tool", return_value=huge):
            self._run(opener)
        payload = json.loads(opener.requests[1]["messages"][-1]["content"])
        self.assertEqual(payload["error"], "tool_result_too_large")

    def test_tool_loop_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LAW_REVIEW_TOOL_LOOP", None)
            self.assertFalse(tool_loop.tool_loop_enabled())
        with patch.dict(os.environ, {"LAW_REVIEW_TOOL_LOOP": "on"}):
            self.assertTrue(tool_loop.tool_loop_enabled())


class ChatToolLoopIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-loop-chat-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)

    def _chat(self):
        from app.review.services import chat

        return chat(1, "张某是否批准了年化12%的固定回报宣传？", "本机律师", None, True, False)

    def test_enabled_loop_answer_is_used_and_recorded(self):
        loop_result = {
            "answer": "根据材料 [资料1]。\n\n请律师复核原文与上述结论。",
            "rounds": 2,
            "tool_calls": 1,
            "tool_trace": [{"tool": "search", "ok": True}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "model": "test-model",
        }
        with patch("app.review.services.tool_loop_enabled", return_value=True), \
                patch("app.review.tool_loop.run_tool_loop", return_value=loop_result):
            result = self._chat()
        self.assertTrue(result["llm_used"])
        self.assertEqual(result["context"]["tools"]["rounds"], 2)
        self.assertEqual(result["context"]["tools"]["tool_calls"], 1)

    def test_unsupported_endpoint_falls_back_to_single_shot(self):
        with patch("app.review.services.tool_loop_enabled", return_value=True), \
                patch("app.review.tool_loop.run_tool_loop", side_effect=ToolsUnsupportedError("no tools")), \
                patch("app.review.services.call_local_llm", return_value="回退答案 [资料1]。\n\n请律师复核原文与上述结论。"):
            result = self._chat()
        self.assertTrue(result["llm_used"])
        self.assertIn("回退答案", result["answer"])
        self.assertNotIn("tools", result["context"])


if __name__ == "__main__":
    unittest.main()
