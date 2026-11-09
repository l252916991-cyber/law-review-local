"""Isolated MCP process probe and explicitly opted-in loopback model acceptance.

The stdlib probe is deliberately not labelled Cursor/Claude Desktop acceptance.
Only synthetic seed data is used; live execution has a fixed request budget.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def loopback_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("验收仅允许明确的 HTTP 回环模型端点")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("端点不能包含凭据、查询或片段")
    return value.rstrip("/")


@contextmanager
def isolated_environment(directory: Path, endpoint: str, model: str, fallback: str) -> Iterator[None]:
    values = {
        "LAW_REVIEW_DATA_DIR": str(directory),
        "LAW_REVIEW_DATABASE_URL": "",  # isolated SQLite file, never a configured PostgreSQL
        "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(directory / "checkpoints.sqlite"),
        "LAW_REVIEW_AUTH_MODE": "local", "LAW_REVIEW_ALLOW_REMOTE_MODELS": "off",
        "LAW_REVIEW_LLM_URL": endpoint, "LAW_REVIEW_LLM_MODEL": model,
        "LAW_REVIEW_LLM_MAX_TOKENS": "700", "LAW_REVIEW_LLM_TIMEOUT": "90",
        "LAW_REVIEW_TOOL_LOOP": "off", "LAW_REVIEW_LLM_FALLBACK": "on" if fallback else "off",
        "LAW_REVIEW_LLM_FALLBACK_URL": endpoint, "LAW_REVIEW_LLM_FALLBACK_MODEL": fallback,
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def probe_mcp(other_document: int) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "lexvault-stdlib-probe", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "documents", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "delete_case", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "get_page", "arguments": {"document_id": other_document, "page_no": 1}}},
    ]
    command = [sys.executable, "-m", "app.integration.mcp_server", "--case-id", "1"]
    rows: list[dict[str, Any]] = []
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, cwd=ROOT) as process:
        assert process.stdin is not None and process.stdout is not None
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" in message:
                if not select.select([process.stdout], [], [], 15)[0]:
                    raise TimeoutError("MCP process did not respond")
                row = json.loads(process.stdout.readline())
                if row.get("id") != message["id"]:
                    raise ValueError("MCP response id mismatch or notification response")
                rows.append(row)
        process.stdin.close()
        process.wait(timeout=15)
    by_id = {row["id"]: row["result"] for row in rows}
    normal = json.loads(by_id[3]["content"][0]["text"])
    token = subprocess.run(command, env={**os.environ, "LAW_REVIEW_AUTH_MODE": "token"},
                           input="", text=True, capture_output=True, timeout=30, cwd=ROOT)
    checks = {
        "process_exit": process.returncode == 0, "notifications_silent": len(rows) == 5,
        "initialize": by_id[1]["protocolVersion"] == "2024-11-05",
        "tool_catalog": len(by_id[2]["tools"]) == 9,
        "normal_call": normal["case_id"] == 1 and normal["count"] == 5 and bool(normal["data_notice"]),
        "unknown_tool_denied": by_id[4].get("isError") is True,
        "cross_case_denied": by_id[5].get("isError") is True and "PRIVATE_CASE_SENTINEL" not in json.dumps(rows),
        "token_start_denied": token.returncode == 2,
    }
    return {"ok": all(checks.values()), "client": "stdlib subprocess protocol probe",
            "desktop_client_verified": False, "checks": checks, "negotiated_version": by_id[1]["protocolVersion"]}


def live_models() -> dict[str, Any]:
    from app.review.chat import chat
    from app.review.generation import last_llm_usage
    from app.review.tool_loop import run_tool_loop

    result: dict[str, Any] = {"real_model": True, "tools": {}, "chat": {}}
    started = time.perf_counter()
    try:
        value = run_tool_loop(case_id=1, question="请先调用 case_overview 核对本案卷宗，再回答有多少份卷宗。不要猜测。",
                              route="事实检索", contexts=[], use_remote_embeddings=False, timeout=90, max_rounds=3)
        result["tools"] = {"ok": value["tool_calls"] > 0, **value}
    except Exception as exc:
        cause = exc.__cause__
        detail = cause.read(3000).decode("utf-8", "replace") if cause and hasattr(cause, "read") else ""
        result["tools"] = {"ok": False, "error_type": type(exc).__name__, "error": str(exc), "detail": detail}
    result["tools"]["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    try:
        value = chat(1, "张某如何参与年化12%固定回报宣传？", "合成验收", None, True, False)
        result["chat"] = {"llm_used": value.get("llm_used"), "validation": value.get("validation"),
                          "provenance": value.get("provenance"), "diagnostic": value.get("failure_diagnostic"),
                          "model_route": (value.get("provenance") or {}).get("route"), "usage_observation": last_llm_usage(),
                          "answer_status": value.get("answer_status"), "answer": value.get("answer")}
    except Exception as exc:
        result["chat"] = {"error_type": type(exc).__name__, "error": str(exc)}
    result["chat"]["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    from app.storage.db import connect
    with connect() as conn:
        result["calls"] = [dict(row) for row in conn.execute(
            "SELECT kind,model,status,input_tokens,output_tokens,latency_ms FROM llm_calls ORDER BY id")]
    result["complete"] = bool(result["tools"].get("ok") and result["chat"].get("llm_used")
                              and (result["chat"].get("model_route") or {}).get("fallback")
                              and {"chat", "chat_fallback"}.issubset({row["kind"] for row in result["calls"]})
                              and len({row["model"] for row in result["calls"] if row["model"]}) >= 2)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="explicitly allow at most 3 tool turns + 2 answer calls")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default="Qwythos-9B-v2-OptiQ-4bit")
    parser.add_argument("--fallback-model", default="")
    args = parser.parse_args(argv)
    endpoint = loopback_endpoint(args.endpoint)
    with tempfile.TemporaryDirectory(prefix="lexvault-acceptance-") as directory:
        with isolated_environment(Path(directory), endpoint, args.model, args.fallback_model):
            from app.storage.db import db_scope, init_db, now, transaction
            with db_scope(Path(directory) / "law_review.db"):
                init_db(seed=True)
                with transaction() as conn:
                    other = conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)",
                                         ("PRIVATE_CASE_SENTINEL", now(), now())).lastrowid
                    document = conn.execute("INSERT INTO documents(case_id,name,created_at,updated_at) VALUES (?,?,?,?)",
                                            (other, "PRIVATE_CASE_SENTINEL", now(), now())).lastrowid
                report = {"synthetic_data_only": True, "endpoint": endpoint, "model": args.model,
                          "fallback_model": args.fallback_model, "mcp": probe_mcp(document),
                          "models": live_models() if args.live else {"executed": False}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mcp_ok": report["mcp"]["ok"], "live": args.live,
                      "models_complete": report["models"].get("complete"), "output": str(args.output)}))
    return 0 if report["mcp"]["ok"] and (not args.live or report["models"].get("complete")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
