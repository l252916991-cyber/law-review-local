"""OpenAI-compatible model test double for page-flow verification.

Nothing here is a model: answers are fixed strings chosen by a marker in the
question, embeddings are deterministic hashes, and rerank scores are positional.
It exists so the streaming protocol, the citation contract and the UI can be
exercised without a real model, and every report that uses it must say so.

Scenarios (selected by the request):

* ordinary question -> an answer with one valid ``[资料1]`` marker and the
  lawyer-review reminder, streamed in several chunks with a usage frame
* question containing ``拒答校验`` -> an answer with no citation marker, which
  the host's contract must reject and retract
* a request that offers ``tools`` and has not seen a tool result yet -> one
  ``get_page`` tool call, so the governed read-only loop can be exercised; the
  follow-up request carries the tool message and gets the final answer

Endpoints: ``POST /v1/chat/completions`` (SSE or JSON per ``stream``),
``POST /v1/embeddings``, ``POST /v1/rerank``, ``GET /v1/models``.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


EMBEDDING_DIMENSIONS = 384
VALID_ANSWER = "根据卷宗第1页的记录，张某批准了年化12%的固定回报宣传[资料1]。请律师复核原文与上述结论。"
INVALID_ANSWER = "张某已经批准了该宣传，材料可以支持这个结论。"
EMBEDDING_MODEL = "stub-embedding"
CHAT_MODEL = "stub-chat"
FALLBACK_MODEL = "fallback-model"


def _vector(text: str) -> list[float]:
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=32).digest()
    raw = [(byte / 255.0) - 0.5 for byte in (digest * 12)[:EMBEDDING_DIMENSIONS]]
    norm = sum(value * value for value in raw) ** 0.5 or 1.0
    return [value / norm for value in raw]


class StubModel:
    """Threaded test double; records every prompt it was asked to answer."""

    def __init__(self, *, chunk_delay: float = 0.05, retract_marker: str = "拒答校验",
                 repeatable_fallback: bool = False) -> None:
        self.chunk_delay = chunk_delay
        self.retract_marker = retract_marker
        self.repeatable_fallback = repeatable_fallback
        self.prompts: list[str] = []
        self.mode = "stub"
        # The fallback scenario needs one rejected answer before the next call
        # succeeds, so the host's one-shot fallback really runs.
        self.invalid_served = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "StubModel":
        model = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep the flow output readable
                return

            def _json(self, payload: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path.rstrip("/").endswith("/models"):
                    self._json({"object": "list", "data": [
                        {"id": CHAT_MODEL, "object": "model", "owned_by": "lexvault-stub"},
                        {"id": EMBEDDING_MODEL, "object": "model", "owned_by": "lexvault-stub"},
                        {"id": FALLBACK_MODEL, "object": "model", "owned_by": "lexvault-stub"},
                    ]})
                    return
                self._json({"error": "not found"}, status=404)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._json({"error": "invalid json"}, status=400)
                    return
                path = self.path.rstrip("/")
                if path.endswith("/chat/completions"):
                    model._answer(self, payload)
                elif path.endswith("/embeddings"):
                    inputs = payload.get("input") or []
                    if isinstance(inputs, str):
                        inputs = [inputs]
                    self._json({"object": "list", "model": EMBEDDING_MODEL, "data": [
                        {"object": "embedding", "index": index, "embedding": _vector(str(text))}
                        for index, text in enumerate(inputs)
                    ]})
                elif path.endswith("/rerank"):
                    documents = payload.get("documents") or []
                    self._json({"model": "stub-rerank", "results": [
                        {"index": index, "relevance_score": round(1.0 / (index + 1), 6)}
                        for index in range(len(documents))
                    ]})
                else:
                    self._json({"error": "not found"}, status=404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="stub-model", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("stub model is not running")
        address = self._server.server_address
        host = str(address[0])
        port = int(address[1])
        return "http://" + host + ":" + str(port) + "/v1"

    # -- chat --------------------------------------------------------------
    def _answer(self, handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
        messages = payload.get("messages") or []
        prompt = "\n".join(str(item.get("content") or "") for item in messages if isinstance(item, dict))
        self.prompts.append(prompt)
        if payload.get("stream") is False:
            self._json_answer(handler, prompt, messages, payload)
            return
        answer = self._pick_answer(prompt, str(payload.get("model") or ""))
        chunks = [answer[index:index + 8] for index in range(0, len(answer), 8)] or [answer]
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()

        def frame(data: dict[str, Any]) -> None:
            handler.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
            handler.wfile.flush()

        for chunk in chunks:
            frame({"id": "stub-1", "object": "chat.completion.chunk", "model": CHAT_MODEL,
                   "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]})
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
        frame({"id": "stub-1", "object": "chat.completion.chunk", "model": CHAT_MODEL,
               "choices": [], "usage": {"prompt_tokens": 128, "completion_tokens": 48}})
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
        handler.close_connection = True

    def _pick_answer(self, prompt: str, model_name: str = "") -> str:
        """Choose the scripted answer; the fallback scenario rejects once."""
        if "回退验证" in prompt and self.repeatable_fallback:
            return VALID_ANSWER if model_name == FALLBACK_MODEL else INVALID_ANSWER
        if "回退验证" in prompt and self.invalid_served < 1:
            self.invalid_served += 1
            return INVALID_ANSWER
        return INVALID_ANSWER if self.retract_marker in prompt else VALID_ANSWER

    def _json_answer(self, handler: BaseHTTPRequestHandler, prompt: str, messages: list[Any],
                     payload: dict[str, Any]) -> None:
        """Non-streaming answer: one tool call first, then the grounded answer."""
        has_tool_result = any(
            isinstance(item, dict) and item.get("role") == "tool" for item in messages
        )
        offers_tools = bool(payload.get("tools"))
        message: dict[str, Any] = {"role": "assistant", "content": ""}
        if offers_tools and not has_tool_result:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "stub-call-1", "type": "function",
                    "function": {"name": "get_page",
                                 "arguments": json.dumps({"document_id": 1, "page_no": 1})},
                }],
            }
        else:
            message["content"] = self._pick_answer(prompt, str(payload.get("model") or ""))
        body = json.dumps({
            "id": "stub-json-1", "object": "chat.completion", "model": CHAT_MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 96, "completion_tokens": 32},
        }, ensure_ascii=False).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        handler.close_connection = True
