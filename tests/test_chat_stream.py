"""Streaming transport: token frames are provisional until the contract passes."""
import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main as main_module
from app import services
from app.chat_idempotency import (
    CANCELLED,
    FAILED,
    ChatCancelled,
    begin as begin_chat_request,
    finish as finish_chat_request,
    lookup as lookup_chat_request,
)
from app.db import connect, db_scope, init_db
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
    """The event contract: provisional tokens, validated citations, terminal done."""

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
        self.payload = {
            "question": "张某是否批准了年化12%的固定回报宣传？",
            "use_llm": False, "use_remote_embeddings": False,
        }

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

    def _stream(self, payload: dict) -> list[tuple[str, dict]]:
        with self.client.stream("POST", self.url, json=payload) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            return self._events(response)

    def _message_count(self) -> int:
        with connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def test_success_ends_with_citations_final_and_done(self):
        events = self._stream(self.payload)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[0], "status")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("citations", kinds)
        self.assertIn("final", kinds)
        self.assertLess(kinds.index("citations"), kinds.index("final"))
        self.assertLess(kinds.index("final"), kinds.index("done"))
        self.assertNotIn("token", kinds)
        done = dict(events)["done"]
        self.assertEqual(done["status"], "ok")
        self.assertIsNotNone(done["conversation_id"])
        self.assertIsNotNone(done["message_id"])
        # Chat turns never create agent_runs, so a run id would be a fiction.
        self.assertNotIn("run_id", done)
        self.assertIsNone(done["usage"])
        self.assertIsNone(done["ttft_ms"])
        final = dict(events)["final"]
        self.assertIn("answer", final)
        self.assertFalse(final["llm_used"])
        citations = dict(events)["citations"]
        self.assertEqual(citations["count"], len(citations["citations"]))

    def test_phases_and_provisional_tokens_precede_the_citations(self):
        def fake_chat(*args, **kwargs):
            kwargs["on_phase"]("generating")
            kwargs["on_token"]("第一段")
            kwargs["on_token"]("第二段")
            kwargs["on_phase"]("validating")
            return {
                "answer": "第一段第二段[资料1]", "citations": [{"index": 1, "page": 2}],
                "llm_used": True, "conversation_id": 7, "answer_status": "answered",
                "citation_check": "passed",
            }

        with patch.object(main_module, "chat", side_effect=fake_chat):
            events = self._stream({"question": "测试流式", "use_llm": True, "use_remote_embeddings": False})
        kinds = [kind for kind, _ in events]
        self.assertEqual(
            kinds, ["status", "status", "token", "token", "status", "citations", "final", "done"]
        )
        phases = [data["phase"] for kind, data in events if kind == "status"]
        self.assertEqual(phases, ["retrieval", "generating", "validating"])
        self.assertTrue(events[2][1]["provisional"])
        self.assertEqual(events[5][1]["count"], 1)
        done = dict(events)["done"]
        self.assertEqual(done["conversation_id"], 7)
        self.assertIsNone(done["message_id"])
        self.assertEqual(done["answer_status"], "answered")
        # Tokens were emitted, so a first-token measurement exists.
        self.assertIsNotNone(done["ttft_ms"])

    def test_failed_contract_retracts_and_done_reports_failure(self):
        def failing_chat(*args, **kwargs):
            kwargs["on_token"]("SECRET-DRAFT")
            raise RuntimeError("model_output_failed_validation")

        with patch.object(main_module, "chat", side_effect=failing_chat):
            events = self._stream({"question": "测试撤回", "use_llm": True, "use_remote_embeddings": False})
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["status", "token", "retract", "done"])
        retract = dict(events)["retract"]
        self.assertEqual(retract["code"], "model_error")
        self.assertNotIn("SECRET-DRAFT", json.dumps(retract, ensure_ascii=False))
        done = dict(events)["done"]
        self.assertEqual(done["status"], "failed")
        self.assertTrue(done["retracted"])
        self.assertNotIn("SECRET-DRAFT", json.dumps(done, ensure_ascii=False))

    def test_chat_turns_do_not_create_agent_runs(self):
        with connect() as conn:
            before = int(conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0])
        self._stream(self.payload)
        with connect() as conn:
            after = int(conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0])
        self.assertEqual(before, after)


class ChatRequestIdempotencyTests(unittest.TestCase):
    """Retries must replay a committed turn and never append a second one."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-chat-idem-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.request_id = "client-request-0001"
        self.url = "/api/cases/1/chat/stream"
        self.payload = {
            "question": "张某是否批准了年化12%的固定回报宣传？",
            "use_llm": False, "use_remote_embeddings": False,
            "request_id": self.request_id,
        }

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

    def _stream(self, payload: dict) -> list[tuple[str, dict]]:
        with self.client.stream("POST", self.url, json=payload) as response:
            self.assertEqual(response.status_code, 200)
            return self._events(response)

    def _messages(self) -> int:
        with connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def test_retry_replays_the_committed_turn_without_duplicating_messages(self):
        first = self._stream(self.payload)
        first_final = dict(first)["final"]
        after_first = self._messages()
        self.assertEqual(after_first, 2)

        second = self._stream(self.payload)
        second_kinds = [kind for kind, _ in second]
        self.assertEqual(second_kinds[0], "status")
        self.assertEqual(dict(second)["status"]["phase"], "replayed")
        second_final = dict(second)["final"]
        self.assertEqual(second_final["answer"], first_final["answer"])
        self.assertEqual(second_final["conversation_id"], first_final["conversation_id"])
        done = dict(second)["done"]
        self.assertTrue(done["replayed"])
        self.assertEqual(done["conversation_id"], first_final["conversation_id"])
        self.assertEqual(done["message_id"], dict(first)["done"]["message_id"])
        # Per-turn metrics are not re-invented on replay.
        self.assertIsNone(done["ttft_ms"])
        self.assertIsNone(done["usage"])
        self.assertEqual(self._messages(), after_first)

    def test_a_different_request_id_appends_a_new_turn(self):
        self._stream(self.payload)
        self._stream({**self.payload, "request_id": "client-request-0002"})
        self.assertEqual(self._messages(), 4)

    def test_request_id_is_scoped_to_its_case(self):
        self._stream(self.payload)
        other = self.client.post("/api/cases", json={"title": "另一案件的重复键"}).json()["id"]
        with self.client.stream(
            "POST", f"/api/cases/{other}/chat/stream",
            json={**self.payload},
        ) as response:
            self.assertEqual(response.status_code, 200)
            events = self._events(response)
        # The other case must run a real turn instead of replaying case 1's answer.
        self.assertNotEqual(dict(events)["status"]["phase"], "replayed")
        self.assertIn("final", [kind for kind, _ in events])

    def test_in_flight_duplicate_is_refused(self):
        begin_chat_request(1, "client-request-inflight")
        response = self.client.post(self.url, json={**self.payload, "request_id": "client-request-inflight"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("正在处理", response.json()["detail"])

    def test_failed_request_can_run_again_with_the_same_id(self):
        begin_chat_request(1, "client-request-retry")
        finish_chat_request(1, "client-request-retry", FAILED)
        events = self._stream({**self.payload, "request_id": "client-request-retry"})
        self.assertNotEqual(dict(events)["status"]["phase"], "replayed")
        self.assertEqual(dict(events)["done"]["status"], "ok")
        self.assertEqual(self._messages(), 2)

    def test_non_streaming_endpoint_replays_a_committed_turn(self):
        payload = {"question": "张某是否批准了年化12%的固定回报宣传？", "use_llm": False,
                   "use_remote_embeddings": False, "request_id": "client-request-rest-1"}
        first = self.client.post("/api/cases/1/chat", json=payload)
        self.assertEqual(first.status_code, 200)
        messages_after_first = self._messages()
        second = self.client.post("/api/cases/1/chat", json=payload)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["conversation_id"], first.json()["conversation_id"])
        self.assertTrue(second.json()["request_id_replayed"])
        self.assertEqual(self._messages(), messages_after_first)

    def test_in_progress_conflict_is_reported_by_the_non_streaming_endpoint(self):
        begin_chat_request(1, "client-request-rest-inflight")
        response = self.client.post("/api/cases/1/chat", json={
            "question": "测试重复提交", "use_llm": False, "use_remote_embeddings": False,
            "request_id": "client-request-rest-inflight",
        })
        self.assertEqual(response.status_code, 409)


class ChatCancellationTests(unittest.TestCase):
    """A disconnect before the commit boundary must not write a partial turn."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-chat-cancel-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)

    def _counts(self) -> tuple[int, int]:
        with connect() as conn:
            messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            calls = int(conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])
        return messages, calls

    def test_cancel_before_the_model_call_writes_nothing(self):
        before = self._counts()
        with self.assertRaises(Exception) as raised:
            services.chat(1, "张某是否批准了年化12%的固定回报宣传？", "测试律师", None, True,
                          use_remote_embeddings=False, should_cancel=lambda: True)
        self.assertEqual(type(raised.exception).__name__, "ChatCancelled")
        self.assertEqual(self._counts(), before)

    def test_cancel_after_the_model_call_keeps_the_ledger_but_no_messages(self):
        from app import agents

        calls = {"n": 0}

        def cancel_after_model() -> bool:
            calls["n"] += 1
            return calls["n"] >= 2

        valid = {"valid": True, "scope": "test", "checks": {}, "issues": [],
                 "semantic_entailment_checked": False, "amount_calculations_checked": False,
                 "lawyer_review_required": True}
        before_messages, before_calls = self._counts()
        with patch.object(services, "call_local_llm", return_value="根据卷宗，结论如下[资料1]。"),                 patch.object(agents, "validate_review_answer", return_value=valid):
            with self.assertRaises(Exception) as raised:
                services.chat(1, "张某是否批准了年化12%的固定回报宣传？", "测试律师", None, True,
                              use_remote_embeddings=False, should_cancel=cancel_after_model)
        self.assertEqual(type(raised.exception).__name__, "ChatCancelled")
        messages, llm_calls = self._counts()
        self.assertEqual(messages, before_messages)
        # The inference really happened, so the accounting record stays.
        self.assertEqual(llm_calls, before_calls + 1)

    def test_disconnected_stream_cancels_the_request_without_writing(self):
        """Endpoint glue: the generator's exit sets the cancel flag the worker reads."""
        request_id = "cancel-endpoint-0001"

        class DisconnectedRequest:
            async def is_disconnected(self) -> bool:
                return True

        def blocked_chat(*args, **kwargs):
            # Stand-in for a model call that is already in flight when the client
            # leaves; the worker must notice the flag before committing.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not kwargs["should_cancel"]():
                time.sleep(0.01)
            raise ChatCancelled("client gone")

        before = self._counts()

        async def drain() -> list:
            response = await main_module.case_chat_stream(
                1,
                main_module.ChatRequest(
                    question="异步取消验证", use_llm=True, use_remote_embeddings=False,
                    request_id=request_id,
                ),
                DisconnectedRequest(),  # type: ignore[arg-type]
            )
            return [chunk async for chunk in response.body_iterator]

        with patch.object(main_module, "chat", side_effect=blocked_chat):
            asyncio.run(drain())

        deadline = time.monotonic() + 10
        state = None
        while time.monotonic() < deadline:
            state = lookup_chat_request(1, request_id)
            if state is not None and state.state == CANCELLED:
                break
            time.sleep(0.02)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.state, CANCELLED)
        # No half-written turn, and no model call to account for in this stand-in.
        self.assertEqual(self._counts(), before)

    def test_commit_wins_when_the_cancel_flag_flips_after_the_check(self):
        """A disconnect that loses the race still leaves one complete turn."""
        before_messages, _ = self._counts()
        flags = {"n": 0}

        def never_cancel() -> bool:
            flags["n"] += 1
            return False

        result = services.chat(1, "张某是否批准了年化12%的固定回报宣传？", "测试律师", None, False,
                               use_remote_embeddings=False, should_cancel=never_cancel)
        messages, _ = self._counts()
        self.assertEqual(messages, before_messages + 2)
        self.assertIsNotNone(result["conversation_id"])


if __name__ == "__main__":
    unittest.main()
