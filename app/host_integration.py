"""Opt-in synthetic E12/E13 engineering control, outside product routing.

The host stores associations and a rebuildable append-only event projection.
Only the pinned wheel schedules nodes, reserves usage and consumes approvals.
No framework imports enter the host interpreter.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import subprocess
import time
import zipfile
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .db import connect, get_db_path, now, transaction
from .forge_bridge import ROOT, SidecarProtocolError, _read_message, _send, framework_binding
from .forge_runtime import resolve_framework_python
from .host_writes import (
    ISOLATED_RUNTIME,
    LocalHostWritePort,
    authorize,
    canonical,
    digest,
    isolated_guard,
)
from .runtime_ports import (
    AccessDeniedError,
    ContractValidationError,
    IdentityMismatchError,
    RequestConflictError,
    RequestInProgressError,
    RunRequest,
    RunScope,
    UnsupportedCapabilityError,
    WriteAction,
    WriteRequest,
    compute_request_digest,
)
from .security import actor_name, current_principal

WIRE_VERSION = 2
HOST_EVENT_VERSION = 1
GRAPH_VERSION = "synthetic-host-writes-v1"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "exhausted"})
STATUSES = TERMINAL | {"running", "awaiting_approval"}


@contextmanager
def worker_slot(directory: Path, request_id: str):
    """OS leases bound concurrent processes, durable rows bound total admission."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / (digest(request_id) + ".owner")).open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RequestInProgressError("request has an active worker") from exc
        with closing((directory / "slot-0").open("a")) as first, closing((directory / "slot-1").open("a")) as second:
            for slot in (first, second):
                try:
                    fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    yield
                finally:
                    fcntl.flock(slot, fcntl.LOCK_UN)
                return
            raise RequestInProgressError("two workers already active")


class IsolatedHostIntegration:
    """Engineering API only. Start is accept + drive; no automatic background queue."""

    runtime = ISOLATED_RUNTIME
    graph_version = GRAPH_VERSION
    wire_version = WIRE_VERSION
    app_version = "isolated-host-v1"
    application_id = "lexvault-host-isolated"
    profile = "synthetic-v1"
    port_type = LocalHostWritePort
    _database_guard = staticmethod(isolated_guard)

    def __init__(self, *, framework_python: str | Path | None = None, timeout_seconds: float = 120,
                 require_execution_approval: bool = False):
        if not 0 < timeout_seconds <= 600:
            raise ContractValidationError("timeout_seconds must be in (0,600]")
        self.path = get_db_path().resolve()
        self.python = framework_python
        self.timeout = timeout_seconds
        self.require_approval = require_execution_approval
        self.binding = framework_binding()[0]
        with closing(connect()) as conn:
            self._database_guard(conn)

    def _path_guard(self) -> None:
        if self.path != get_db_path().resolve():
            raise AccessDeniedError("database binding changed")

    def accept(self, request: RunRequest) -> dict[str, Any]:
        self._path_guard()
        request.validate()
        self._validate_request(request)

        principal = current_principal()
        if asdict(request.actor) != {"name": principal.name, "admin": principal.admin, "local": principal.local}:
            raise IdentityMismatchError("asserted actor differs from authenticated actor")
        return self._accept_validated(request, principal)

    def _validate_request(self, request):
        if (request.runtime != self.runtime or request.execution_mode != "sync"
                or request.model.use_llm or request.model.model is not None
                or request.model.timeout_seconds is not None or request.use_remote_embeddings
                or request.budget is not None or request.memory_snapshot is not None):
            raise UnsupportedCapabilityError("isolated fixture supports only fixed offline engineering execution")

    def _accept_validated(self, request, principal):
        port = self.port_type(request.scope)
        fingerprint = compute_request_digest(request, principal_name=principal.name,
                                             effective_author=actor_name(request.user_name))
        binding, _ = framework_binding()
        if request.framework_version not in {"", binding["version_label"]} or request.app_version not in {"", self.app_version}:
            raise UnsupportedCapabilityError("unsupported requested application/framework version")
        with transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = port._guard(conn, request.scope, write=True)
            if request.data_snapshot_version and request.data_snapshot_version != version:
                raise RequestConflictError("requested source version differs")
            existing = conn.execute("SELECT * FROM runtime_requests WHERE request_id=?", (request.request_id,)).fetchone()
            if existing:
                if (existing["request_digest"] != fingerprint or existing["principal_name"] != principal.name
                        or existing["case_id"] != request.scope.case_id or existing["runtime"] != self.runtime):
                    raise RequestConflictError("request identity conflict")
                link = conn.execute("SELECT * FROM host_runtime_links WHERE request_id=?", (request.request_id,)).fetchone()
                if link is None or link["source_version"] != version:
                    raise RequestConflictError("request requires reconciliation against its original source")
                return self._handle(conn, request.request_id, duplicate=True)
            count = conn.execute("SELECT COUNT(*) FROM host_runtime_links WHERE status NOT IN ('succeeded','failed','cancelled','exhausted')").fetchone()[0]
            if count >= 4:
                raise RequestInProgressError("isolated admission limit is four")
            receipt = port.execute_in(conn, WriteRequest(WriteAction.RUN_RECORD, request.scope,
                                      request.request_id + ":run", {"question": request.question.strip()}, version))
            run_id = json.loads(receipt.detail)["row_id"]
            conversation = conn.execute("INSERT INTO conversations(case_id,user_name,title,created_at) VALUES (?,?,?,?)",
                (request.scope.case_id, actor_name(request.user_name), request.question[:60], now())).lastrowid
            conn.execute("INSERT INTO runtime_requests(request_id,case_id,runtime,protocol_version,run_id,job_id,created_at,request_digest,principal_name) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (request.request_id, request.scope.case_id, self.runtime,
                         request.protocol_version, run_id, request.request_id, now(), fingerprint, principal.name))
            actions = self._actions(run_id, conversation, request)
            pinned = {"framework": binding["version_label"], "wheel": binding["wheel_sha256"],
                      "protocol": self.wire_version, "graph": self.graph_version, "actor": port.principal_key,
                      "approval": self.require_approval, "timeout_seconds": self.timeout,
                      **self._extra_binding(request)}
            conn.execute("INSERT INTO host_runtime_links(request_id,framework_run_id,conversation_id,source_version,graph_json,binding_json,deadline) "
                         "VALUES (?,?,?,?,?,?,?)", (request.request_id, "host-" + digest([port.principal_key, request.request_id, request.scope.case_id]),
                          conversation, version, canonical(actions), canonical(pinned), time.time() + self.timeout))
            return self._handle(conn, request.request_id)

    def _extra_binding(self, request):
        return {}

    def _actions(self, run_id: int, conversation: int, request: RunRequest) -> list[dict[str, Any]]:
        # Explicit synthetic fixture, NOT the legal review pipeline or a quality result.
        answer = "合成宿主接入验证：仅验证持久写入与恢复，不构成法律结论。"
        actions = [
            ("user-message", WriteAction.CONVERSATION_MESSAGE, {"conversation_id": conversation,
                "role": "user", "content": request.question.strip(), "citations": []}, ()),
            ("step", WriteAction.STEP_RECORD, {"run_id": run_id, "node_name": "synthetic-check",
                "output": {"synthetic": True}}, ("user-message",)),
            ("gap", WriteAction.GAP_DETECTION, {"run_id": run_id, "gap_type": "合成验证", "severity": "低",
                "description": "仅用于隔离恢复测试", "suggestion": "人工复核", "affected_evidence_ids": []}, ("user-message",)),
        ]
        if request.persist_memory:
            actions.append(("draft", WriteAction.MEMORY_DRAFT, {"run_id": run_id, "content": answer,
                            "importance": 0.5}, ("step", "gap")))
        actions.extend([
            ("assistant-message", WriteAction.CONVERSATION_MESSAGE, {"conversation_id": conversation,
                "role": "assistant", "content": answer, "citations": []}, ("draft",) if request.persist_memory else ("step", "gap")),
            ("audit", WriteAction.AUDIT_ENTRY, {"detail": "合成宿主闭环 " + request.request_id}, ("assistant-message",)),
        ])
        return [{"id": name, "action": action.value, "payload": payload, "dependencies": list(deps)}
                for name, action, payload, deps in actions]

    def _load(self, conn, request_id: str, *, writing: bool = False):
        self._path_guard()
        self._database_guard(conn)
        row = conn.execute("SELECT r.case_id,r.run_id,r.principal_name,r.runtime,l.* FROM runtime_requests r "
                           "JOIN host_runtime_links l USING(request_id) WHERE request_id=?", (request_id,)).fetchone()
        if row is None or row["runtime"] != self.runtime:
            raise AccessDeniedError("request unavailable")
        scope = RunScope(row["case_id"])
        actor = authorize(scope, write=writing)
        binding = json.loads(row["binding_json"])
        expected = {"protocol": self.wire_version, "graph": self.graph_version,
                    "framework": self.binding["version_label"], "wheel": self.binding["wheel_sha256"]}
        if any(binding.get(key) != value for key, value in expected.items()):
            raise UnsupportedCapabilityError("saved runtime identity is unsupported; no fallback")
        if actor != binding["actor"] or current_principal().name != row["principal_name"]:
            raise AccessDeniedError("request unavailable")
        if self.port_type._source_version(conn, scope, writing=writing) != row["source_version"]:
            raise RequestConflictError("source changed; historical success is invalid")
        conversation = conn.execute("SELECT archived_at FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
        if row["run_id"] is None or conversation is None or conversation[0] is not None:
            raise RequestConflictError("mapped business resources unavailable")
        return row

    def _project_domain(self, conn, row, result):
        pass

    def _handle(self, conn, request_id: str, *, duplicate: bool = False) -> dict[str, Any]:
        row = self._load(conn, request_id)
        return {"request_id": request_id, "job_id": request_id, "run_id": row["run_id"],
                "framework_run_id": row["framework_run_id"], "conversation_id": row["conversation_id"],
                "status": row["status"], "cursor": row["cursor"], "duplicate": duplicate,
                "cancel_requested": bool(row["cancel_requested"]), "result": json.loads(row["result_json"]),
                "needs_reconciliation": row["status"] in {"running", "unknown"},
                "engineering_only": True}

    def status(self, request_id: str) -> dict[str, Any]:
        self._path_guard()
        with closing(connect()) as conn:
            conn.execute("BEGIN")
            return self._handle(conn, request_id)

    def cancel(self, request_id: str) -> dict[str, Any]:
        self._path_guard()
        with transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load(conn, request_id, writing=True)
            if row["status"] not in TERMINAL:
                # Acceptance is durable; termination is confirmed by framework events.
                conn.execute("UPDATE host_runtime_links SET cancel_requested=1 WHERE request_id=?", (request_id,))
                if row["status"] == "queued":
                    # drive changes queued -> running under this same DB lock
                    # before starting a process. Thus no dispatch can exist here.
                    conn.execute("UPDATE host_runtime_links SET status='cancelled' WHERE request_id=?", (request_id,))
                    conn.execute("UPDATE agent_runs SET status='failed',finished_at=? WHERE id=?", (now(), row["run_id"]))
            return self._handle(conn, request_id)

    def _project(self, request_id: str, events: list[dict[str, Any]], result: dict[str, Any], *, rebuild: bool = False) -> None:
        with transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load(conn, request_id)
            if rebuild:
                conn.execute("DELETE FROM host_runtime_events WHERE request_id=?", (request_id,))
            for event in events:
                seq, event_id = event.get("revision"), event.get("event_id")
                if (event.get("host_event_version") != HOST_EVENT_VERSION or type(seq) is not int or seq < 1
                        or not isinstance(event_id, str) or event.get("status") not in STATUSES):
                    raise SidecarProtocolError("invalid durable event")
                payload = canonical(event)
                old = conn.execute("SELECT payload_json FROM host_runtime_events WHERE request_id=? AND (seq=? OR event_id=?)",
                                   (request_id, seq, event_id)).fetchall()
                if old:
                    if len(old) != 1 or old[0][0] != payload:
                        raise SidecarProtocolError("durable event identity conflict")
                else:
                    conn.execute("INSERT INTO host_runtime_events VALUES (?,?,?,?)", (request_id, seq, event_id, payload))
            cursor, status = (0, "running") if rebuild else (row["cursor"], row["status"])
            while True:
                event = conn.execute("SELECT payload_json FROM host_runtime_events WHERE request_id=? AND seq=?",
                                     (request_id, cursor + 1)).fetchone()
                if event is None:
                    break
                next_status = json.loads(event[0])["status"]
                if status in TERMINAL and next_status != status:
                    raise SidecarProtocolError("terminal projection cannot regress")
                status, cursor = next_status, cursor + 1
            # A missing event never proves completion or releases admission.
            if result.get("revision") != cursor or result.get("status") != status:
                raise SidecarProtocolError("incomplete event projection")
            conn.execute("UPDATE host_runtime_links SET cursor=?,status=?,result_json=? WHERE request_id=?",
                         (cursor, status, canonical(result), request_id))
            if status in TERMINAL:
                conn.execute("UPDATE agent_runs SET status=?,final_answer=?,finished_at=CASE WHEN finished_at='' THEN ? ELSE finished_at END WHERE id=?",
                    ("completed" if status == "succeeded" else "failed", result.get("final_output") or "",
                     now(), row["run_id"]))
            self._project_domain(conn, row, result)

    def rebuild_projection(self, request_id: str) -> dict[str, Any]:
        return self.drive(request_id, command="inspect", _rebuild=True)

    def purge_deleted_state(self) -> int:
        """Drain isolated lifecycle outbox; never delete an active worker's files.

        Each request owns its entire synthetic framework directory. Source case
        erasure already cascades its mapping/receipts; outbox survives that cascade.
        """
        self._path_guard()
        authorize(RunScope(1), manage=True)
        if not current_principal().admin:
            raise AccessDeniedError("global isolated cleanup requires an administrator")
        with closing(connect()) as conn:
            self._database_guard(conn)
            rows = conn.execute("SELECT * FROM host_runtime_cleanup").fetchall()
        removed = 0
        for row in rows:
            run_id = row["framework_run_id"]
            if not re.fullmatch(r"host-[a-f0-9]{64}", run_id):
                raise ContractValidationError("invalid lifecycle cleanup identity")
            try:
                with worker_slot(self.path.parent / "host-worker-leases", row["request_id"]):
                    directory = self.path.parent / "host-framework-state" / run_id
                    if directory.is_symlink():
                        raise AccessDeniedError("refusing symbolic cleanup directory")
                    if directory.exists():
                        shutil.rmtree(directory)
                    with transaction() as conn:
                        conn.execute("DELETE FROM host_runtime_cleanup WHERE framework_run_id=?", (run_id,))
                    removed += 1
            except RequestInProgressError:
                continue
        return removed

    def drive(self, request_id: str, *, command: str = "recover", _rebuild: bool = False) -> dict[str, Any]:
        """New process always uses find/inspect before start; absence alone never retries a tool."""
        self._path_guard()
        if command not in {"recover", "inspect", "approve"}:
            raise ContractValidationError("unsupported isolated control command")
        with closing(connect()) as conn:
            conn.execute("BEGIN")
            row = self._load(conn, request_id, writing=command != "inspect")
            pinned = json.loads(row["binding_json"])
        binding, wheel = framework_binding()
        expected = {"framework": binding["version_label"], "wheel": binding["wheel_sha256"],
                    "protocol": self.wire_version, "graph": self.graph_version}
        if any(pinned.get(key) != value for key, value in expected.items()):
            raise UnsupportedCapabilityError("saved runtime identity is unsupported; no fallback")
        if command == "approve":
            authorize(RunScope(row["case_id"]), write=True, approval=True)
        directory = self.path.parent / "host-framework-state" / row["framework_run_id"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with worker_slot(self.path.parent / "host-worker-leases", request_id):
            with transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self._load(conn, request_id, writing=command != "inspect")
                if row["status"] == "cancelled" and row["cursor"] == 0:
                    return self._handle(conn, request_id)
                if row["status"] in {"queued", "exhausted"} and row["cursor"] == 0 and time.time() >= row["deadline"]:
                    # Under the same lock as queued -> running, so this is a
                    # proven queue expiry, before any framework dispatch.
                    conn.execute("UPDATE host_runtime_links SET status='exhausted' WHERE request_id=?", (request_id,))
                    conn.execute("UPDATE agent_runs SET status='failed',finished_at=CASE WHEN finished_at='' THEN ? ELSE finished_at END WHERE id=?",
                                 (now(), row["run_id"]))
                    return self._handle(conn, request_id)
                if command != "inspect" and row["status"] == "queued":
                    conn.execute("UPDATE host_runtime_links SET status='running' WHERE request_id=?", (request_id,))
            try:
                result = self._sidecar(row, pinned, binding, wheel, directory, command)
            except (SidecarProtocolError, TimeoutError, OSError):
                with transaction() as conn:
                    conn.execute("UPDATE host_runtime_links SET status='unknown' WHERE request_id=? "
                                 "AND status NOT IN ('succeeded','failed','cancelled','exhausted')", (request_id,))
                raise
            self._project(request_id, result["events"], result["result"], rebuild=_rebuild)
        return self.status(request_id)

    def _sidecar(self, row, pinned, binding, wheel, directory, command):
        python = resolve_framework_python(self.python).absolute()
        with zipfile.ZipFile(wheel) as archive:
            files = {n: hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist()
                     if n.startswith("forgeharness/") and n.endswith(".py")}
        RunScope(row["case_id"])  # validates the persisted case id before launch
        actions = {a["id"]: a for a in json.loads(row["graph_json"])}
        token = secrets.token_hex(32)
        # Control/reconciliation may run after the original deadline; no new tool
        # is then admitted. This finite pipe deadline does not renew the run budget.
        deadline = time.monotonic() + max(60, min(pinned["timeout_seconds"] + 5, row["deadline"] - time.time() + 5))
        with (directory / "stderr.log").open("ab") as stderr:
            process = subprocess.Popen([str(python), "-I", str(ROOT / "app/host_integration_worker.py")],
                cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"})
            try:
                _send(process, {"wire_version": self.wire_version, "profile": self.profile,
                    "token": token, "package_files": files,
                    "version": binding["version_label"], "run_id": row["framework_run_id"],
                    "actor_id": pinned["actor"], "case_id": row["case_id"], "source_version": row["source_version"],
                    "actions": list(actions.values()), "graph_version": self.graph_version, "command": command,
                    "timeout_seconds": pinned["timeout_seconds"], "deadline": row["deadline"],
                    "require_approval": pinned["approval"], "cancel": bool(row["cancel_requested"]),
                    **self._worker_configuration(row)})
                seq = 0
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    pending = bytearray()
                    while True:
                        message = _read_message(process, selector, pending, deadline)
                        if message.get("wire_version") != self.wire_version or message.get("token") != token:
                            raise SidecarProtocolError("sidecar authentication or version mismatch")
                        if message.get("type") == "result":
                            if message.get("artifact_verified") is not True or message.get("trace_valid") is not True:
                                raise SidecarProtocolError("artifact or trace verification failed")
                            if message.get("result", {}).get("status") == "succeeded" and time.time() >= row["deadline"]:
                                # The execution deadline does not expire an identical,
                                # already projected success. Artifact/trace verification
                                # and current authorization still apply to every read.
                                known_success = (row["status"] == "succeeded"
                                    and json.loads(row["result_json"]) == message["result"])
                                if not known_success:
                                    raise TimeoutError("late framework completion requires reconciliation")
                            break
                        if message.get("seq") != seq + 1:
                            raise SidecarProtocolError("sidecar sequence mismatch")
                        seq += 1
                        kind = message.get("type")
                        with closing(connect()) as conn:
                            conn.execute("BEGIN")
                            current = self._load(conn, row["request_id"], writing=kind == "write")
                        response: dict[str, Any] = {"ok": True, "cancel": bool(current["cancel_requested"])}
                        if kind in {"write", "receipt"}:
                            action = actions.get(message.get("node"))
                            if action is None:
                                raise SidecarProtocolError("unregistered host action")
                            # Framework invocation key binds the exact saved graph + run.
                            # The host selects the action and payload, never pipe content.
                            key = message.get("key")
                            if key != self._invocation_key(row, action["id"]):
                                raise SidecarProtocolError("invalid invocation key")
                            response.update(self._invoke_action(row, action, key, receipt_only=kind == "receipt"))
                        elif kind == "start":
                            with transaction() as conn:
                                conn.execute("BEGIN IMMEDIATE")
                                current = self._load(conn, row["request_id"], writing=True)
                                allowed = (not current["start_authorized"] and not current["cancel_requested"]
                                           and time.time() < current["deadline"])
                                if allowed:
                                    conn.execute("UPDATE host_runtime_links SET start_authorized=1 WHERE request_id=?", (row["request_id"],))
                                response["start_allowed"] = allowed
                        elif kind != "validate":
                            response.update(self._extra_operation(row, kind, message))
                        _send(process, {"wire_version": self.wire_version, "token": token, "seq": seq, **response})
                assert process.stdin is not None
                process.stdin.close()
                if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
                    raise SidecarProtocolError("sidecar exited unsuccessfully")
                return message
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None and not pipe.closed:
                        pipe.close()

    def _invocation_key(self, row, node_id):
        namespace = hashlib.sha256(canonical([self.application_id, "lexvault-local"]).encode()).hexdigest()[:24]
        internal_id = namespace + "." + hashlib.sha256(row["framework_run_id"].encode()).hexdigest()
        return internal_id + ":" + node_id

    def _extra_operation(self, row, kind, message):
        raise SidecarProtocolError("unknown sidecar operation")

    def _worker_configuration(self, row):
        return {}

    def _invoke_action(self, row, action, key, *, receipt_only):
        scope = RunScope(row["case_id"])
        port = self.port_type(scope)
        response = {}
        if receipt_only:
            receipt = port.query_receipt(scope, WriteAction(action["action"]), key)
        else:
            with transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = self._load(conn, row["request_id"], writing=True)
                response["cancel"] = bool(current["cancel_requested"])
                if current["cancel_requested"] or time.time() >= row["deadline"]:
                    response["denied"] = "cancelled_or_expired"
                    receipt = None
                else:
                    receipt = port.execute_in(conn, WriteRequest(WriteAction(action["action"]), scope, key,
                                              action["payload"], row["source_version"]))
        return {**response, "receipt": asdict(receipt) if receipt else None}
