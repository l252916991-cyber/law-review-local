"""Minimal MCP server (stdio, JSON-RPC 2.0) exposing the read-only case tools.

MCP's stdio transport is newline-delimited JSON-RPC, so this server needs no
third-party dependency and stays inside the project's supply-chain rules.

Design limits:

* only the read-only tool allowlist is exposed — never a write, export, approval
  or model call;
* the case is bound at process start (``--case-id``), so the client cannot ask
  for another case and the tool layer keeps its own scope check;
* a token-authenticated deployment has no client identity on stdin, so the
  server refuses to start unless access control is local. Bridging a remote
  identity over stdio is a separate, explicit piece of work.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, TextIO

from app.review.agent_tools import READONLY_TOOL_NAMES, TOOL_SCHEMAS, execute_readonly_tool

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "lexvault-case-tools", "version": "0.1.0"}


def tool_definitions() -> list[dict[str, Any]]:
    """Convert OpenAI function schemas into MCP tool descriptors."""
    definitions: list[dict[str, Any]] = []
    for name in sorted(TOOL_SCHEMAS):
        function = TOOL_SCHEMAS[name]["function"]
        definitions.append(
            {
                "name": name,
                "description": function["description"],
                "inputSchema": function["parameters"],
            }
        )
    return definitions


class McpServer:
    def __init__(self, case_id: int, *, use_remote_embeddings: bool = False):
        if case_id < 1:
            raise ValueError("case_id 必须是正整数")
        self.case_id = case_id
        self.use_remote_embeddings = use_remote_embeddings

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC message. Notifications return ``None``."""
        if not isinstance(message, dict):
            return _error(None, -32600, "invalid request")
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str):
            return _error(request_id, -32600, "invalid request")
        if "id" not in message:
            # Notification (for example notifications/initialized): no response.
            return None
        params = message.get("params") or {}
        try:
            if method == "initialize":
                return _result(
                    request_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                    },
                )
            if method == "ping":
                return _result(request_id, {})
            if method == "tools/list":
                return _result(request_id, {"tools": tool_definitions()})
            if method == "tools/call":
                return _result(request_id, self._call_tool(params))
            return _error(request_id, -32601, f"method not found: {method}")
        except Exception as exc:  # never leak case data in the message
            return _error(request_id, -32603, f"internal error ({type(exc).__name__})")

    def _call_tool(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in READONLY_TOOL_NAMES:
            return _tool_error("unsupported_tool", sorted(READONLY_TOOL_NAMES))
        if not isinstance(arguments, dict):
            return _tool_error("invalid_arguments", "arguments must be an object")
        try:
            result = execute_readonly_tool(
                self.case_id, name, arguments, use_remote_embeddings=self.use_remote_embeddings
            )
        except KeyError:
            return _tool_error("unsupported_tool", sorted(READONLY_TOOL_NAMES))
        except Exception as exc:
            return _tool_error("tool_failed", f"{type(exc).__name__}: {str(exc)[:200]}")
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(reason: str, detail: Any) -> dict[str, Any]:
    """Tool failures are results with ``isError``, not transport errors."""
    return {
        "content": [{"type": "text", "text": json.dumps({"error": reason, "detail": detail}, ensure_ascii=False)}],
        "isError": True,
    }


def serve(server: McpServer, stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LexVault read-only MCP server")
    parser.add_argument("--case-id", type=int, default=None, help="bound case id (or LAW_REVIEW_MCP_CASE_ID)")
    parser.add_argument("--list-tools", action="store_true", help="print tool descriptors and exit")
    parser.add_argument("--allow-remote-embeddings", action="store_true")
    args = parser.parse_args(argv)

    if args.list_tools:
        print(json.dumps(tool_definitions(), ensure_ascii=False, indent=2))
        return 0

    mode = os.getenv("LAW_REVIEW_AUTH_MODE", "local").strip().lower()
    if mode != "local":
        print(
            "MCP stdio 只能在本机访问模式下运行：token 模式没有可传递的客户端身份。",
            file=sys.stderr,
        )
        return 2

    case_id = args.case_id
    if case_id is None:
        raw = os.getenv("LAW_REVIEW_MCP_CASE_ID", "").strip()
        if not raw.isdigit():
            print("必须通过 --case-id 或 LAW_REVIEW_MCP_CASE_ID 绑定案件", file=sys.stderr)
            return 2
        case_id = int(raw)

    from contextlib import closing

    from app.storage.db import connect, system_access

    with system_access(), closing(connect()) as conn:
        walled = conn.execute("SELECT 1 FROM case_walls WHERE case_id = ?", (case_id,)).fetchone() is not None
    if walled:
        # The stdio client has no identity, so it can never be inside a wall.
        print("该案件设有信息隔离墙，MCP 无法确认调用者身份，拒绝启动", file=sys.stderr)
        return 2

    server = McpServer(case_id, use_remote_embeddings=args.allow_remote_embeddings)
    serve(server, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
