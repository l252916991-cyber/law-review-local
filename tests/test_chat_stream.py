"""Streaming transport: token frames are provisional until the contract passes."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main as main_module
from app import services
from app.db import db_scope, init_db
from app.main import app
from app.services import read_sse_with_deadline


class _FakeResponse:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)
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


class _FakeOpener:
    def __init__(self, payload: bytes):
        self.payload = payload

    def open(self, request, timeout=None):
        return _FakeResponse(self.payload)


def _sse(*frames: str) -> bytes:
    return "".join(f"data: {frame}\n\n" for frame in frames).encode("utf-8")


class SSEReaderTests(unittest.TestCase):
    def test_concatenates_deltas_and_captures_usage(self):
        body = _sse(
            json.dumps({"choices": [{"delta": {"content": "根据"}}]}),
            json.dumps({"choices": [{"delta": {"content": "材料"}}]}),
            json.dumps({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}),
            "[DONE]",
        )
        seen: list[str] = []
        text, usage = read_sse_with_deadline(_FakeResponse(body), 5, on_event=lambda frame: seen.append(frame["content"]))
        self.assertEqual(text, "根据材料")
        self.assertEqual(seen, ["根据", "材料"])
        self.assertEqual(usage, {"prompt_tokens": 9, "completion_tokens": 2})

    def test_ignores_keepalive_and_malformed_frames(self):
        body = b": ping\n\ndata: not-json\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\ndata: [DONE]\n\n"
        text, usage = read_sse_with_deadline(_FakeResponse(body), 5)
        self.assertEqual(text, "ok")
        self.assertIsNone(usage)

    def test_stops_at_done_and_ignores_trailing_frames(self):
        body = _sse(
            json.dumps({"choices": [{"delta": {"content": "a"}}]}),
            "[DONE]",
            json.dumps({"choices": [{"delta": {"content": "b"}}]}),
        )
        text, _ = read_sse_with_deadline(_FakeResponse(body), 5)
        self.assertEqual(text, "a")

    def test_size_limit_fails_closed(self):
        body = _sse(json.dumps({"choices": [{"delta": {"content": "x" * 200}}]}))
        with self.assertRaises(ValueError):
            read_sse_with_deadline(_FakeResponse(body), 5, max_bytes=64)

    def test_absolute_deadline_applies_before_the_first_read(self):
        with self.assertRaises(TimeoutError):
            read_sse_with_deadline(_FakeResponse(b""), 5, deadline=0)


class StreamingModelCallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-stream-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)

    def _call(self, body: bytes) -> tuple[str, list[str]]:
        tokens: list[str] = []
        with patch.object(services, "local_llm_available", return_value=(True, "test-model")), \
                patch.object(services, "egress_opener", return_value=_FakeOpener(body)):
            answer = services.call_local_llm(
                "问题", "事实检索", [{"name": "卷宗", "page_no": 1, "quote": "原文", "document_id": 1}],
                on_token=tokens.append,
            )
        return answer, tokens

    def test_streams_tokens_and_reports_first_token_latency(self):
        body = _sse(
            json.dumps({"choices": [{"delta": {"content": "根据"}}]}),
            json.dumps({"choices": [{"delta": {"content": "材料[资料1]"}}]}),
            json.dumps({"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}),
            "[DONE]",
        )
        answer, tokens = self._call(body)
        self.assertEqual(answer, "根据材料[资料1]")
        self.assertEqual(tokens, ["根据", "材料[资料1]"])
        observed = services._last_llm_usage.get()
        assert observed is not None
        self.assertEqual(observed["model"], "test-model")
        self.assertIsNotNone(observed["ttft_ms"])

    def test_reasoning_block_is_never_forwarded_to_the_client(self):
        body = _sse(
            json.dumps({"choices": [{"delta": {"content": "<think>"}}]}),
            json.dumps({"choices": [{"delta": {"content": "hidden scratchpad"}}]}),
            json.dumps({"choices": [{"delta": {"content": "</think>答案"}}]}),
            "[DONE]",
        )
        answer, tokens = self._call(body)
        self.assertEqual(answer, "答案")
        self.assertEqual("".join(tokens), "答案")
        self.assertNotIn("hidden", "".join(tokens))


class ChatStreamEndpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-chat-stream-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = "/api/cases/1/chat/stream"

    @staticmethod
    def _events(response) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        event = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((event, json.loads(line[len("data: "):])))
        return events

    def test_status_precedes_the_answer_and_no_token_is_sent_without_a_model(self):
        with self.client.stream(
            "POST", self.url,
            json={"question": "张某是否批准了年化12%的固定回报宣传？", "use_llm": False, "use_remote_embeddings": False},
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            events = self._events(response)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[0], "status")
        self.assertIn("final", kinds)
        self.assertNotIn("token", kinds)
        final = dict(events)["final"]
        self.assertIn("answer", final)
        self.assertFalse(final["llm_used"])

    def test_provisional_tokens_precede_the_final_frame(self):
        def fake_chat(*args, **kwargs):
            on_token = kwargs["on_token"]
            on_token("第一段")
            on_token("第二段")
            return {"answer": "第一段第二段", "citations": [], "llm_used": True, "conversation_id": 7}

        with patch.object(main_module, "chat", side_effect=fake_chat):
            with self.client.stream(
                "POST", self.url, json={"question": "测试流式", "use_llm": True},
            ) as response:
                events = self._events(response)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["status", "token", "token", "final"])
        self.assertTrue(events[1][1]["provisional"])
        self.assertEqual(events[-1][1]["answer"], "第一段第二段")

    def test_failed_contract_retracts_instead_of_finalising(self):
        def failing_chat(*args, **kwargs):
            kwargs["on_token"]("SECRET-DRAFT")
            raise RuntimeError("model_output_failed_validation")

        with patch.object(main_module, "chat", side_effect=failing_chat):
            with self.client.stream(
                "POST", self.url, json={"question": "测试撤回", "use_llm": True},
            ) as response:
                events = self._events(response)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["status", "token", "retract"])
        self.assertEqual(events[-1][1]["code"], "model_error")
        self.assertNotIn("SECRET-DRAFT", json.dumps(events[-1][1], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
