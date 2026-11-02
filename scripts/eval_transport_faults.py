"""Exercise real model transports against an explicitly allowed loopback fault server.

This is controlled fault injection, not a model-quality benchmark. Each fault
runs through the actual ablation generator and through chat persistence on a new
synthetic database. A separate process budget enforces task deadlines; this does
not claim that every application task has an internal cancellation guarantee.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import select
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MODEL = "controlled-fault-model"
QUESTION = "请列出合成借款材料中的借款本金记载。"
SOURCE = "合成借款材料：借款本金为人民币128000元。本页仅用于本地传输故障测试。"
ANSWER = "合成借款材料记载的借款本金为人民币128000元[资料1]。请律师复核原文与上述结论。"
FAULTS = ("interface_5xx", "task_deadline", "return_timeout", "truncated_return")
PATHS = ("ablation_generation", "chat_persistence")
CLIENT_SOURCES = ("app/services.py", "app/collaboration_ablation.py")


def source_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CLIENT_SOURCES}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def local_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
            and parsed.port is not None and parsed.path == "/v1"
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            "Only the controlled 127.0.0.1 HTTP endpoint is allowed")
    return value


def failure_class(exc: BaseException) -> tuple[str, list[str]]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not value for value in chain) and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = [type(value).__name__ for value in chain]
    if any(isinstance(value, urllib.error.HTTPError) and value.code >= 500 for value in chain):
        return "interface_5xx", names
    if any(isinstance(value, TimeoutError) for value in chain):
        return "return_timeout", names
    if any(isinstance(value, (json.JSONDecodeError, http.client.IncompleteRead)) for value in chain):
        return "truncated_return", names
    return "unexpected_client_error", names


class FaultEndpoint:
    """A real TCP server with fixed, separately recorded transport stages."""

    def __init__(self, mode: str) -> None:
        require(mode in (*FAULTS, "healthy"), "Unknown fault mode")
        self.mode = mode
        self.events: list[dict[str, Any]] = []
        self.release = threading.Event()
        # Set when the generation request arrives: the task deadline is timed
        # from the moment the model call is actually hanging.
        self.generation_started = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FaultEndpoint:
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def reply(self, body: bytes, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/models":
                    self.reply(b"{}", 404)
                    return
                endpoint.events.append({"stage": "models_probe"})
                self.reply(json.dumps({"data": [{"id": MODEL}]}).encode())

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/chat/completions":
                    self.reply(b"{}", 404)
                    return
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(raw)
                endpoint.events.append({"stage": "generation_received", "model": payload.get("model"),
                                        "stream": payload.get("stream"), "request_bytes": len(raw)})
                endpoint.generation_started.set()
                if endpoint.mode == "interface_5xx":
                    endpoint.events.append({"stage": "headers_sent", "http_status": 503})
                    self.reply(b'{"error":"injected_service_unavailable"}', 503)
                    return
                if endpoint.mode == "task_deadline":
                    # No response headers: the caller's larger socket timeout
                    # cannot pre-empt the evaluator's shorter overall deadline.
                    endpoint.release.wait(15)
                    self.close_connection = True
                    return
                body = json.dumps({"model": MODEL, "choices": [{"message": {"content": ANSWER}}],
                                   "usage": {"prompt_tokens": 20, "completion_tokens": 20}},
                                  ensure_ascii=False).encode()
                if endpoint.mode == "healthy":
                    endpoint.events.append({"stage": "complete_body_sent", "bytes": len(body)})
                    self.reply(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                endpoint.events.append({"stage": "headers_sent", "http_status": 200})
                # Contains answer text but lacks the remaining enclosing JSON.
                # A raw prefix must never become a successful answer.
                prefix = body[:body.index(b'"usage"') - 4]
                self.wfile.write(prefix)
                self.wfile.flush()
                endpoint.events.append({"stage": "partial_body_sent", "bytes": len(prefix),
                                        "declared_bytes": len(body), "contains_scripted_answer": ANSWER.encode() in prefix})
                if endpoint.mode == "return_timeout":
                    endpoint.release.wait(15)
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        require(self._server is not None, "Fault server is not running")
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __exit__(self, *exc: Any) -> None:
        self.release.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def database_state(directory: Path) -> dict[str, Any]:
    path = directory / "law_review.db"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return {"assistant_messages": db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0],
                "integrity_ok": db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]}


def worker(endpoint: str, directory: Path, path: str, timeout: int) -> int:
    local_endpoint(endpoint)
    require(path in PATHS, "Unknown client path")
    require(not (directory / "law_review.db").exists(), "Worker database must be new")
    for key in list(os.environ):
        if key.startswith("LAW_REVIEW_"):
            del os.environ[key]
    os.environ.update(LAW_REVIEW_DATA_DIR=str(directory), LAW_REVIEW_LLM_URL=endpoint,
                      LAW_REVIEW_LLM_MODEL=MODEL, LAW_REVIEW_LLM_TIMEOUT=str(timeout),
                      LAW_REVIEW_EMBEDDING_URL="http://127.0.0.1:1/v1", LAW_REVIEW_RERANK_URL="http://127.0.0.1:1/v1",
                      LAW_REVIEW_TOOL_LOOP="off", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
    sys.path.insert(0, str(ROOT))
    from app.collaboration_ablation import live_generator
    from app.db import init_db, now, transaction
    from app.services import chat

    init_db(seed=False)
    with transaction() as db:
        stamp = now()
        db.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES(1,?,?,?)", ("合成传输异常评测", stamp, stamp))
        db.execute("INSERT INTO documents(id,case_id,name,pages,created_at,updated_at) VALUES(1,1,?,1,?,?)",
                   ("合成借款材料.txt", stamp, stamp))
        db.execute("INSERT INTO pages(document_id,page_no,text) VALUES(1,1,?)", (SOURCE,))
    print(json.dumps({"stage": "ready", "loaded_client_sha256": source_hashes(),
                      "python": sys.version.split()[0]}), flush=True)
    require(sys.stdin.readline().strip() == "GO", "Worker start handshake missing")
    result: dict[str, Any] = {"success_reported": False, "raw_answer_received": False,
                              "llm_used": False, "failure_class": None, "exception_types": []}
    started = time.monotonic()
    try:
        if path == "ablation_generation":
            value = live_generator(MODEL, timeout, endpoint)(QUESTION, "事实检索", [
                {"document_id": 1, "name": "合成借款材料.txt", "page_no": 1, "quote": SOURCE}], "")
            answer, llm_used = value.text, True
        else:
            response = chat(1, QUESTION, "合成故障评测", None, True, False)
            answer, llm_used = str(response.get("answer", "")), response.get("llm_used") is True
            result.update(answer_status=response.get("answer_status"), answer_route=response.get("route"),
                          answer_mode=response.get("provenance", {}).get("mode"))
        result.update(success_reported=True, raw_answer_received=bool(answer), llm_used=llm_used,
                      answer_sha256=hashlib.sha256(answer.encode()).hexdigest())
    except Exception as exc:
        category, names = failure_class(exc)
        result.update(failure_class=category, exception_types=names)
    result.update(client_elapsed_seconds=time.monotonic() - started, **database_state(directory))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def run_attempt(mode: str, path: str, *, timeout: int = 1, task_deadline: float = .35) -> dict[str, Any]:
    require(mode in (*FAULTS, "healthy") and path in PATHS, "Unknown fault mode or client path")
    require(timeout >= 1 and 0 < task_deadline < timeout, "Task budget must be shorter than positive client timeout")
    with tempfile.TemporaryDirectory(prefix="lexvault-transport-fault-") as temporary, FaultEndpoint(mode) as endpoint:
        directory = Path(temporary)
        command = [sys.executable, str(Path(__file__).resolve()), "--allow-loopback-fault-server", "--worker",
                   "--endpoint", endpoint.url, "--worker-data", str(directory), "--client-path", path,
                   "--client-timeout", str(timeout)]
        with (directory / "stderr.log").open("w") as errors:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=errors, text=True)
            try:
                assert process.stdout is not None
                readable, _, _ = select.select([process.stdout], [], [], 20)
                require(bool(readable), "Fault worker failed startup")
                ready = json.loads(process.stdout.readline())
                require(ready.get("stage") == "ready", "Fault worker failed startup")
                started = time.monotonic()
                limit = task_deadline if mode == "task_deadline" else timeout + 5
                killed = False
                handshake: str | None = "GO\n"
                if mode == "task_deadline":
                    # Retrieval and prompt assembly before the model call take a
                    # variable fraction of a second on a loaded machine; timing the
                    # 0.35 s budget from GO let it expire before the hang began.
                    # The budget now covers the hung generation, which is the fault.
                    assert process.stdin is not None
                    process.stdin.write("GO\n")
                    process.stdin.flush()
                    handshake = None
                    if not endpoint.generation_started.wait(timeout + 5):
                        limit = 0.01  # never reached generation: fail below, do not wait
                    started = time.monotonic()
                try:
                    raw, _ = process.communicate(handshake, timeout=limit)
                    require(process.returncode == 0, "Fault worker exited unexpectedly")
                    result = json.loads(raw.strip().splitlines()[-1])
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=3)
                    killed = True
                    result = {"success_reported": False, "raw_answer_received": False, "llm_used": False,
                              "failure_class": "task_deadline", "exception_types": ["EvaluatorProcessDeadline"]}
                elapsed = time.monotonic() - started
                result.update(**database_state(directory))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)
        events = list(endpoint.events)
    post_seen = any(event["stage"] == "generation_received" for event in events)
    partial_seen = any(event["stage"] == "partial_body_sent" for event in events)
    persisted = result["assistant_messages"] > 0
    if mode == "healthy":
        passed = (post_seen and result["success_reported"] and result["llm_used"]
                  and result["raw_answer_received"] and result["integrity_ok"]
                  and persisted == (path == "chat_persistence") and not killed)
    else:
        passed = (post_seen and result["failure_class"] == mode and not result["success_reported"]
                  and not result["raw_answer_received"] and not persisted and result["integrity_ok"]
                  and (mode not in {"return_timeout", "truncated_return"} or partial_seen)
                  and (mode != "task_deadline" or killed and elapsed <= task_deadline + 1.5))
    return {"mode": mode, "client_path": path, "elapsed_seconds": elapsed,
            "task_process_killed": killed, "task_deadline_enforcer": "evaluator_subprocess_budget",
            "loaded_client_sha256": ready["loaded_client_sha256"], "worker_python": ready["python"],
            "endpoint_events": events, "injection_reached_generation": post_seen,
            "successful_answer_persisted": persisted, "partial_output_misclassified_as_success": partial_seen
            and bool(result["success_reported"]), "passed": passed, **result}


def write_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def run(output: Path, *, allow_loopback_fault_server: bool, repeats: int = 5) -> dict[str, Any]:
    require(allow_loopback_fault_server, "Explicit --allow-loopback-fault-server is required")
    require(type(repeats) is int and repeats >= 5, "At least five repetitions per fault and client path are required")
    output = output.resolve()
    require(not output.exists(), "Output must be a new directory")
    output.mkdir(parents=True, mode=0o700)
    initial_sources = source_hashes()
    protocol = {"mode": "controlled_fault_endpoint", "faults": list(FAULTS), "client_paths": list(PATHS),
                "repeats": repeats, "client_timeout_seconds": 1, "task_deadline_seconds": .35,
                "post_ready_deadline_slack_seconds": 1.5, "real_model_quality_evidence": False,
                "client_source_sha256": initial_sources,
                "latency_scope": "GO handshake to result/termination; excludes imports and fixture setup",
                "task_deadline_clock": "starts when the endpoint receives the generation request",
                "independent_business_questions": 0,
                "task_deadline_enforcer": "evaluator_subprocess_budget_not_product_cancellation"}
    write_new(output / "protocol.json", protocol)
    rows = []
    with (output / "attempts.jsonl").open("x", encoding="utf-8") as journal:
        os.chmod(output / "attempts.jsonl", 0o600)
        for mode in ("healthy", *FAULTS):
            for path in PATHS:
                for repeat in range(1 if mode == "healthy" else repeats):
                    row = {"repetition": repeat + 1, **run_attempt(mode, path)}
                    rows.append(row)
                    journal.write(json.dumps(row, ensure_ascii=False) + "\n")
                    journal.flush()
    sources_stable = (initial_sources == source_hashes()
                      and all(row["loaded_client_sha256"] == initial_sources for row in rows))
    result = {"stage": "passed" if all(row["passed"] for row in rows) and sources_stable else "failed", **protocol,
              "source_inputs_stable": sources_stable,
              "attempts": len(rows), "controls": 2, "injected_attempts": len(rows) - 2,
              "failed_checks": [{"mode": row["mode"], "client_path": row["client_path"],
                                 "repetition": row["repetition"]} for row in rows if not row["passed"]],
              "successful_fault_answers_persisted": sum(row["successful_answer_persisted"] for row in rows
                                                         if row["mode"] != "healthy"),
              "partial_outputs_misclassified_as_success": sum(row["partial_output_misclassified_as_success"]
                                                               for row in rows),
              "by_fault_family": {mode: {
                  "attempts": sum(row["mode"] == mode for row in rows),
                  "passed_checks": sum(row["mode"] == mode and row["passed"] for row in rows),
                  "observed_failure_classes": sorted({str(row["failure_class"]) for row in rows if row["mode"] == mode}),
                  "max_elapsed_seconds": max(row["elapsed_seconds"] for row in rows if row["mode"] == mode),
                  "client_path_counts": {path: sum(row["mode"] == mode and row["client_path"] == path for row in rows)
                                         for path in PATHS},
              } for mode in FAULTS},
              "protocol_sha256": hashlib.sha256((output / "protocol.json").read_bytes()).hexdigest(),
              "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "limitations": ["Controlled synthetic endpoint, not real-model quality or repeatability evidence",
                              "Overall task deadlines enforced by the evaluator subprocess budget",
                              "Non-streaming JSON transports only; SSE token delivery is not evaluated",
                              "Each fresh synthetic database is discarded after persistence/integrity checks"]}
    write_new(output / "report.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-loopback-fault-server", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--endpoint", help=argparse.SUPPRESS)
    parser.add_argument("--worker-data", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--client-path", choices=PATHS, default=PATHS[0], help=argparse.SUPPRESS)
    parser.add_argument("--client-timeout", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.allow_loopback_fault_server:
        parser.error("Explicit --allow-loopback-fault-server is required; no production endpoint is supported")
    if args.worker:
        if not args.endpoint or not args.worker_data:
            parser.error("Internal worker requires the controlled endpoint and a new data directory")
        return worker(args.endpoint, args.worker_data.resolve(), args.client_path, args.client_timeout)
    if args.output is None:
        parser.error("--output must name a new private directory")
    result = run(args.output, allow_loopback_fault_server=True, repeats=args.repeats)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["stage"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
