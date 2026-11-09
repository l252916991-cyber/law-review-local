"""E12 isolated prototype of the six frozen HostWritePort actions.

Only a newly created, explicitly marked synthetic database is admitted. There
is no HTTP route or production enable flag. SQLite owns both effect and receipt;
framework checkpoint commits are separate and must reconcile through receipts.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.security import auth_mode, configured_tokens, current_principal, oidc_settings
from app.integration.runtime_ports import (
    DEFAULT_TENANT_ID,
    MAX_PAGE_TEXT_CHARS,
    AccessDeniedError,
    ContractValidationError,
    RequestConflictError,
    RunScope,
    UnsupportedCapabilityError,
    WriteAction,
    WriteReceipt,
    WriteRequest,
)
from app.storage.db import SQLITE_FILE_ONLY, connect, database_url, db_scope, get_db_path, init_db, now, transaction

ISOLATED_RUNTIME = "forgeharness-isolated-v1"
WRITE_SCHEMA_VERSION = 1
MAX_WRITE_BYTES = 200_000


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("payload must be finite JSON") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def create_isolated_database(path: Path) -> None:
    """Never relabel, clone or open an existing business database as synthetic."""
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    with db_scope(path):
        init_db(seed=False)
        with transaction() as conn:
            conn.execute("INSERT INTO host_integration_config VALUES (1,'synthetic-isolated-v1')")


def isolated_guard(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT profile FROM host_integration_config WHERE singleton=1").fetchone()
    if row is None or row[0] != "synthetic-isolated-v1":
        raise AccessDeniedError("host integration requires a new synthetic isolated database")


def authorize(scope: RunScope, *, write: bool = False, approval: bool = False, manage: bool = False) -> str:
    """Resolve current configured rights again, including between sidecar calls."""
    if scope.tenant_id != DEFAULT_TENANT_ID or type(scope.case_id) is not int or scope.case_id < 1:
        raise AccessDeniedError("scope unavailable")
    try:
        principal = current_principal()
        if auth_mode() == "token":
            candidates = list(configured_tokens().values())
            settings = oidc_settings()
            if settings:
                candidates.extend(settings["principals"].values())
            matches = [p for p in candidates if p.name == principal.name]
            if not matches or any(p != matches[0] for p in matches):
                raise AccessDeniedError("principal no longer authorized")
            current = matches[0]
            if (current.admin, current.local) != (principal.admin, principal.local):
                raise AccessDeniedError("principal binding changed")
            principal = current
        if not principal.has("view") or (write and not principal.has("edit")):
            raise AccessDeniedError("permission denied")
        if approval and not principal.has("approve"):
            raise AccessDeniedError("execution approval permission required")
        if manage and not principal.has("manage"):
            raise AccessDeniedError("management permission required")
        if not principal.admin and scope.case_id not in principal.case_ids:
            raise AccessDeniedError("scope unavailable")
    except HTTPException as exc:
        raise AccessDeniedError("authentication required") from exc
    return digest({"name": principal.name, "admin": principal.admin, "local": principal.local})


def source_version(conn: sqlite3.Connection, scope: RunScope, *, writing: bool = False) -> str:
    """Version only INPUT facts, excluding this runtime's own projections.

    This prototype consumes synthetic knowledge rows, not an external corpus.
    A product adapter must additionally bind its actual corpus/model revisions.
    """
    row = conn.execute("SELECT * FROM cases WHERE id=?", (scope.case_id,)).fetchone()
    if row is None or row["lifecycle_status"] == "trash":
        raise AccessDeniedError("case unavailable")
    if writing and row["lifecycle_status"] != "active":
        raise AccessDeniedError("case is read-only")
    size = conn.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(p.text)),0),COALESCE(MAX(LENGTH(p.text)),0) "
                        "FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.case_id=?", (scope.case_id,)).fetchone()
    if size[0] > 500 or size[1] > 2_000_000 or size[2] > MAX_PAGE_TEXT_CHARS:
        raise ContractValidationError("synthetic case exceeds engineering capacity")
    queries = {
        "documents": "SELECT * FROM documents WHERE case_id=? ORDER BY id",
        "pages": "SELECT p.* FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.case_id=? ORDER BY p.id",
        "evidence": "SELECT * FROM evidence WHERE case_id=? ORDER BY id",
        "relations": "SELECT * FROM evidence_relations WHERE case_id=? ORDER BY id",
        "memories": "SELECT * FROM memories WHERE case_id=? AND kind!='runtime_draft' ORDER BY id",
    }
    inputs = {name: [dict(r) for r in conn.execute(sql, (scope.case_id,))] for name, sql in queries.items()}
    inputs["case"] = dict(row)
    inputs["knowledge"] = [dict(r) for r in conn.execute("SELECT * FROM knowledge ORDER BY id")]
    return digest(inputs)


FIELDS = {
    WriteAction.RUN_RECORD: ({"question"}, {"question"}),
    WriteAction.STEP_RECORD: ({"run_id", "node_name", "output"}, {"run_id", "node_name", "output"}),
    WriteAction.GAP_DETECTION: (
        {"run_id", "gap_type", "severity", "description", "suggestion", "affected_evidence_ids"},
        {"run_id", "gap_type", "severity", "description", "suggestion", "affected_evidence_ids"}),
    WriteAction.MEMORY_DRAFT: ({"run_id", "content", "importance"}, {"run_id", "content", "importance"}),
    WriteAction.CONVERSATION_MESSAGE: (
        {"conversation_id", "role", "content", "citations"}, {"conversation_id", "role", "content", "citations"}),
    WriteAction.AUDIT_ENTRY: ({"detail"}, {"detail"}),
}


class LocalHostWritePort:
    """Trusted host object; DB path, actor and scope never come from the pipe."""

    runtime = ISOLATED_RUNTIME
    receipt_version = WRITE_SCHEMA_VERSION
    max_write_bytes = MAX_WRITE_BYTES
    fields = FIELDS
    _database_guard = staticmethod(isolated_guard)

    def __init__(self, scope: RunScope):
        if database_url():
            raise UnsupportedCapabilityError(SQLITE_FILE_ONLY)
        self.scope, self.path = scope, get_db_path().resolve()
        self.principal_key = authorize(scope)
        with closing(connect()) as conn:
            self._database_guard(conn)

    def _guard(self, conn: sqlite3.Connection, scope: RunScope, *, write: bool) -> str:
        if scope != self.scope or get_db_path().resolve() != self.path:
            raise AccessDeniedError("port binding changed")
        if authorize(scope, write=write) != self.principal_key:
            raise AccessDeniedError("principal binding changed")
        self._database_guard(conn)
        return self._source_version(conn, scope, writing=write)

    @classmethod
    def _source_version(cls, conn, scope, *, writing=False):
        return source_version(conn, scope, writing=writing)

    def current_version(self) -> str:
        with closing(connect()) as conn:
            conn.execute("BEGIN")
            return self._guard(conn, self.scope, write=False)

    def _validate(self, request: WriteRequest) -> dict[str, Any]:
        if not isinstance(request.action, WriteAction):
            raise ContractValidationError("unknown write action")
        if (not isinstance(request.idempotency_key, str) or not 1 <= len(request.idempotency_key) <= 256
                or not request.idempotency_key.isascii() or not request.idempotency_key.isprintable()):
            raise ContractValidationError("invalid idempotency key")
        raw = canonical(request.payload)
        if len(raw.encode()) > self.max_write_bytes:
            raise ContractValidationError("write exceeds payload bound")
        payload = json.loads(raw)  # freeze caller-owned mutable dictionaries
        required, allowed = self.fields[request.action]
        if not isinstance(payload, dict) or not required <= payload.keys() or payload.keys() - allowed:
            raise ContractValidationError("invalid action fields")
        for name, value in payload.items():
            if name in {"run_id", "conversation_id"} and (type(value) is not int or value < 1):
                raise ContractValidationError("invalid resource identity")
            if name in {"question", "node_name", "gap_type", "description", "suggestion", "content", "detail"}:
                if not isinstance(value, str) or not value.strip():
                    raise ContractValidationError("nonempty text required")
        if "question" in payload and len(payload["question"]) > 3000:
            raise ContractValidationError("question too long")
        if "node_name" in payload and len(payload["node_name"]) > 96:
            raise ContractValidationError("node name too long")
        if "output" in payload and not isinstance(payload["output"], dict):
            raise ContractValidationError("output must be an object")
        if "severity" in payload and payload["severity"] not in {"低", "中", "高"}:
            raise ContractValidationError("invalid severity")
        if "importance" in payload and (type(payload["importance"]) not in {int, float}
                or not math.isfinite(payload["importance"]) or not 0 <= payload["importance"] <= 1):
            raise ContractValidationError("importance must be in [0,1]")
        if request.action == WriteAction.CONVERSATION_MESSAGE and payload.get("role") not in {"user", "assistant"}:
            raise ContractValidationError("invalid message role")
        self._validate_citation_shape(payload)
        if "affected_evidence_ids" in payload:
            ids = payload["affected_evidence_ids"]
            if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int or i < 1 for i in ids):
                raise ContractValidationError("invalid evidence identities")
        return payload

    def _validate_citation_shape(self, payload):
        if "citations" in payload and payload["citations"] != []:
            raise ContractValidationError("synthetic prototype does not produce validated citations")

    def execute(self, request: WriteRequest) -> WriteReceipt:
        with transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.execute_in(conn, request)

    def execute_in(self, conn: sqlite3.Connection, request: WriteRequest) -> WriteReceipt:
        """Internal composition hook: caller MUST own BEGIN IMMEDIATE."""
        if not conn.in_transaction:
            raise RuntimeError("write requires an explicit host transaction")
        current = self._guard(conn, request.scope, write=True)
        payload = self._validate(request)
        if request.source_version != current:
            raise RequestConflictError("source version changed")
        identity = digest({"v": self.receipt_version, "actor": self.principal_key, "scope": asdict(self.scope),
                           "action": request.action.value, "source": current, "payload": payload})
        row = conn.execute("SELECT * FROM host_write_receipts WHERE action=? AND idempotency_key=?",
                           (request.action.value, request.idempotency_key)).fetchone()
        if row:
            if row["request_digest"] != identity:
                raise RequestConflictError("idempotency binding conflict")
            self._check_effect(conn, row)
            return self._receipt(row, duplicate=True)
        result = self._apply(conn, request.action, payload)
        receipt_id = uuid.uuid4().hex
        conn.execute("INSERT INTO host_write_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                     (receipt_id, request.action.value, request.idempotency_key, self.scope.case_id,
                      self.principal_key, current, identity, canonical(result), now()))
        row = conn.execute("SELECT * FROM host_write_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
        return self._receipt(row)

    @staticmethod
    def _receipt(row: sqlite3.Row, *, duplicate: bool = False) -> WriteReceipt:
        return WriteReceipt(row["receipt_id"], WriteAction(row["action"]), row["idempotency_key"], True,
                            duplicate, "duplicate" if duplicate else "applied", row["result_json"])

    def query_receipt(self, scope: RunScope, action: WriteAction, idempotency_key: str) -> WriteReceipt | None:
        with closing(connect()) as conn:
            conn.execute("BEGIN")
            current = self._guard(conn, scope, write=False)
            row = conn.execute("SELECT * FROM host_write_receipts WHERE action=? AND idempotency_key=?",
                               (action.value, idempotency_key)).fetchone()
            if row is None:
                return None
            if row["case_id"] != scope.case_id or row["principal_key"] != self.principal_key:
                raise AccessDeniedError("receipt unavailable")
            if row["source_version"] != current:
                raise RequestConflictError("receipt source version changed")
            self._check_effect(conn, row)
            return self._receipt(row)

    @staticmethod
    def _effect_version(conn, table: str, row_id: int) -> str:
        if table not in {"agent_runs", "agent_steps", "gap_detections", "memories", "messages", "audit_log"}:
            raise ContractValidationError("unsupported effect table")
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise RequestConflictError("receipt effect unavailable")
        value = dict(row)
        if table == "agent_runs":
            value = {k: value[k] for k in ("id", "case_id", "question", "runtime", "created_at")}
        if table == "messages":
            conversation = conn.execute("SELECT archived_at FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
            if conversation is None or conversation[0] is not None:
                raise RequestConflictError("receipt conversation unavailable")
        return digest(value)

    def _check_effect(self, conn, row) -> None:
        result = json.loads(row["result_json"])
        if result.get("schema_version") != self.receipt_version:
            raise ContractValidationError("unsupported saved receipt schema")
        if self._effect_version(conn, result["table"], result["row_id"]) != result["effect_version"]:
            raise RequestConflictError("receipt effect changed")

    def _apply(self, conn: sqlite3.Connection, action: WriteAction, p: dict[str, Any]) -> dict[str, Any]:
        case, ts = self.scope.case_id, now()
        if "run_id" in p:
            run = conn.execute("SELECT case_id,runtime FROM agent_runs WHERE id=?", (p["run_id"],)).fetchone()
            if run is None or run["case_id"] != case or run["runtime"] != self.runtime:
                raise AccessDeniedError("run unavailable")
        if action == WriteAction.RUN_RECORD:
            row_id = conn.execute(
                "INSERT INTO agent_runs(case_id,question,route,runtime,created_at) VALUES (?,?,?,?,?)",
                (case, p["question"], self.runtime, self.runtime, ts)).lastrowid
            table = "agent_runs"
        elif action == WriteAction.STEP_RECORD:
            row_id = conn.execute(
                "INSERT INTO agent_steps(run_id,node_name,agent_role,status,output_json,started_at,finished_at) "
                "VALUES (?,?,'synthetic','completed',?,?,?)",
                (p["run_id"], p["node_name"], canonical(p["output"]), ts, ts)).lastrowid
            table = "agent_steps"
        elif action == WriteAction.GAP_DETECTION:
            for evidence_id in p["affected_evidence_ids"]:
                if conn.execute("SELECT 1 FROM evidence WHERE id=? AND case_id=?", (evidence_id, case)).fetchone() is None:
                    raise AccessDeniedError("evidence unavailable")
            row_id = conn.execute(
                "INSERT INTO gap_detections(case_id,run_id,gap_type,severity,description,suggestion,affected_evidence_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (case, p["run_id"], p["gap_type"], p["severity"],
                 p["description"], p["suggestion"], canonical(p["affected_evidence_ids"]), ts)).lastrowid
            table = "gap_detections"
        elif action == WriteAction.MEMORY_DRAFT:
            row_id = conn.execute(
                "INSERT INTO memories(case_id,user_name,kind,content,importance,source_run_id,created_at) "
                "VALUES (?,?,'runtime_draft',?,?,?,?)",
                (case, current_principal().name, p["content"], p["importance"], p["run_id"], ts)).lastrowid
            table = "memories"
        elif action == WriteAction.CONVERSATION_MESSAGE:
            conversation = conn.execute("SELECT case_id,archived_at FROM conversations WHERE id=?",
                                        (p["conversation_id"],)).fetchone()
            if conversation is None or conversation["case_id"] != case or conversation["archived_at"] is not None:
                raise AccessDeniedError("conversation unavailable")
            row_id = conn.execute(
                "INSERT INTO messages(conversation_id,role,content,citations_json,route,created_at) VALUES (?,?,?,?,?,?)",
                (p["conversation_id"], p["role"], p["content"], canonical(p["citations"]), self.runtime, ts)).lastrowid
            table = "messages"
        else:
            row_id = conn.execute("INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'隔离宿主写入',?,?)",
                                  (case, canonical({"actor": current_principal().name, "detail": p["detail"]}), ts)).lastrowid
            table = "audit_log"
        return {"schema_version": self.receipt_version, "table": table, "row_id": row_id,
                "effect_version": self._effect_version(conn, table, row_id)}
