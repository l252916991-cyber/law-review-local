"""Original domain algorithms in an explicitly versioned, isolated durable graph."""
from __future__ import annotations

import json
import time
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict

from .agents import PlannerAgent
from .db import connect, transaction
from .domain_writes import DOMAIN_GRAPH, DOMAIN_RUNTIME, DomainHostWritePort, corpus_identity
from .forge_bridge import SidecarProtocolError
from .forge_readonly import ReadonlyCaseSession
from .host_integration import IsolatedHostIntegration
from .host_ports import LocalHostReadPort, capture_case_rows, case_identity
from .host_writes import ISOLATED_RUNTIME, canonical, digest
from .runtime_adapter import current_actor_identity
from .runtime_ports import (
    AccessDeniedError, ContractValidationError, ModelConfig, RequestConflictError,
    RunRequest, RunScope, WriteAction, WriteRequest,
)


class DomainNodeRejected(ValueError):
    def __init__(self, error, backend_calls):
        super().__init__(type(error).__name__)
        self.backend_calls = deepcopy(backend_calls)


def input_rows(rows):
    """Exclude this runtime's outputs, retaining source memories and their FK runs."""
    rows["memories"] = [r for r in rows["memories"] if r["kind"] != "runtime_draft"]
    referenced = {r["source_run_id"] for r in rows["memories"]}
    runs = {r["id"] for r in rows["agent_runs"]
            if r["runtime"] in {DOMAIN_RUNTIME, ISOLATED_RUNTIME} and r["id"] not in referenced}
    rows["agent_runs"] = [r for r in rows["agent_runs"] if r["id"] not in runs]
    rows["gap_detections"] = [r for r in rows["gap_detections"] if r["run_id"] not in runs]
    return rows


class DomainReadPort(LocalHostReadPort):
    def source_identity(self):
        return case_identity(input_rows(capture_case_rows(self.scope.case_id,
            source_path=self.identity_source_path, max_pages=self.max_pages, max_chars=self.max_chars)))


class DomainSession(ReadonlyCaseSession):
    read_port_type = DomainReadPort

    def _capture(self):
        return input_rows(super()._capture())


class IsolatedDomainIntegration(IsolatedHostIntegration):
    runtime = DOMAIN_RUNTIME
    graph_version = DOMAIN_GRAPH
    wire_version = 3
    app_version = "domain-host-v1"
    application_id = "lexvault-domain-isolated"
    profile = "domain-v1"
    port_type = DomainHostWritePort

    def accept(self, request):
        if len(request.question.strip()) > 500:
            raise ContractValidationError("domain query exceeds 500-character search boundary")
        return super().accept(request)

    def _extra_binding(self, request):
        return {"question": request.question.strip(), "persist_memory": request.persist_memory,
                "corpus": corpus_identity()}

    def _actions(self, run_id, conversation, request):
        route, plan = PlannerAgent().build_plan(request.question.strip())
        frozen_plan = {"route": route, "nodes": [asdict(node) for node in plan]}
        actions = [{"id": "user-message", "action": WriteAction.CONVERSATION_MESSAGE.value,
                    "payload": {"conversation_id": conversation, "role": "user", "content": request.question.strip(), "citations": []},
                    "dependencies": []}]

        def node(name, dependencies, **extra):
            actions.append({"id": name, "action": WriteAction.STEP_RECORD.value, "domain_node": name,
                            "dependencies": list(dependencies), **extra})

        node("planner", ["user-message"], frozen_plan=frozen_plan)
        node("memory_recall", ["planner"])
        node("retrieve", ["memory_recall"])
        specialists = [n for n in plan if n.name not in {"retrieve", "critic", "memory"}]
        for spec in specialists:
            node(spec.name, spec.depends_on)
        actions.append({"id": "specialist-join", "kind": "join", "dependencies": [n.name for n in specialists]})
        node("critic", ["specialist-join"])
        node("finish", ["critic"])
        node("persist-effects", ["finish"])
        node("audit", ["persist-effects"])
        return actions

    def _read_outputs(self, conn, row):
        """Read only registered immutable effects and prove composite children too."""
        outputs, completed = {}, set()
        port = self.port_type(RunScope(row["case_id"]))
        for action in json.loads(row["graph_json"]):
            if action.get("kind") == "model":
                continue
            if action.get("kind") == "join":
                if set(action["dependencies"]) <= completed:
                    completed.add(action["id"])
                continue
            receipt = conn.execute("SELECT * FROM host_write_receipts WHERE action=? AND idempotency_key=?",
                (action["action"], self._invocation_key(row, action["id"]))).fetchone()
            if receipt is None:
                continue
            if (receipt["principal_key"] != port.principal_key or receipt["case_id"] != row["case_id"]
                    or receipt["source_version"] != row["source_version"]):
                raise AccessDeniedError("saved domain effect binding differs")
            port._check_effect(conn, receipt)
            if "domain_node" in action:
                step = conn.execute("SELECT status,output_json FROM agent_steps WHERE id=?",
                                    (json.loads(receipt["result_json"])["row_id"],)).fetchone()
                if step["status"] != "completed":
                    continue
                outputs[action["id"]] = json.loads(step["output_json"])["value"]
            completed.add(action["id"])
        return outputs, completed

    @staticmethod
    def _message(row, finished):
        return {"conversation_id": row["conversation_id"], "role": "assistant", "content": finished["answer"],
                "citations": finished["citations"], "strict_answer": finished["strict_answer"],
                "statutory_sources": finished["statutory_sources"]}

    def _compute(self, row, action, outputs):
        name = action["domain_node"]
        if name == "persist-effects":
            return {"persist_memory": json.loads(row["binding_json"])["persist_memory"]}, []
        if name == "audit":
            return {"strict_answer": outputs["finish"]["strict_answer"]}, []
        binding = json.loads(row["binding_json"])
        request = RunRequest(request_id=row["request_id"], scope=RunScope(row["case_id"]),
            actor=current_actor_identity(), question=binding["question"], runtime="forgeharness-readonly",
            model=ModelConfig(use_llm=False), use_remote_embeddings=False, persist_memory=False)
        with self._session(row, request, action) as session:
            session.outputs = self._prepare_outputs(row, action, outputs)
            session.backends.set_deadline(time.monotonic() + max(0, row["deadline"] - time.time()))
            try:
                value = session.call(name)
            except (ValueError, ContractValidationError) as error:
                raise DomainNodeRejected(error, session.backends.events) from error
            backend_calls = deepcopy(session.backends.events)
            if name == "planner" and canonical(value) != canonical(action["frozen_plan"]):
                raise RequestConflictError("planner differs from the saved graph")
        if name == "finish":
            if len(value["strict_answer"]) > 50_000:
                raise ContractValidationError("strict domain result exceeds tool evidence bound")
            with closing(connect()) as conn:
                conn.execute("BEGIN")
                self._load(conn, row["request_id"])
                self.port_type(RunScope(row["case_id"])).verify_sources(conn, self._message(row, value))
        return value, backend_calls

    def _session(self, row, request, action):
        return DomainSession(request)

    def _prepare_outputs(self, row, action, outputs):
        return deepcopy(outputs)

    def _effects(self, row, action, outputs, value):
        if action["id"] == "audit":
            return [("audit", WriteAction.AUDIT_ENTRY, {"detail": "隔离领域审查 " + row["request_id"]})]
        if action["id"] != "persist-effects":
            return []
        finished = outputs["finish"]
        gaps = {canonical(g): g for g in outputs.get("gap_detection", {}).get("gaps", [])}
        if len(gaps) > 100:
            raise ContractValidationError("too many domain gap effects")
        effects = []
        for index, (_, gap) in enumerate(sorted(gaps.items())):
            effects.append((f"gap-{index}", WriteAction.GAP_DETECTION, {"run_id": row["run_id"],
                "gap_type": gap["type"], "severity": gap["severity"], "description": gap["description"],
                "suggestion": gap["suggestion"], "affected_evidence_ids": gap.get("affected_evidence_ids", [])}))
        if value["persist_memory"]:
            effects.append(("draft", WriteAction.MEMORY_DRAFT, {"run_id": row["run_id"],
                            "content": finished["answer"], "importance": 0.5}))
        effects.append(("message", WriteAction.CONVERSATION_MESSAGE, self._message(row, finished)))
        return effects

    def _receipt_reply(self, receipt, action):
        if receipt is None:
            return {"receipt": None}
        with closing(connect()) as conn:
            step = conn.execute("SELECT status,output_json FROM agent_steps WHERE id=?",
                                (json.loads(receipt.detail)["row_id"],)).fetchone()
        saved = json.loads(step["output_json"])
        return {"receipt": asdict(receipt), "tool_ok": step["status"] == "completed",
                "content": saved["value"]["strict_answer"] if action["id"] == "audit" else canonical({
                    "node": action["id"], "receipt_id": receipt.receipt_id,
                    "output_digest": digest(saved["value"]), "status": step["status"]})}

    def _invoke_action(self, row, action, key, *, receipt_only):
        if action.get("kind") == "join":
            raise SidecarProtocolError("join has no host invocation")
        if "domain_node" not in action:
            return super()._invoke_action(row, action, key, receipt_only=receipt_only)
        port = self.port_type(RunScope(row["case_id"]))
        existing = port.query_receipt(port.scope, WriteAction.STEP_RECORD, key)
        if existing is not None or receipt_only:
            return self._receipt_reply(existing, action)
        with closing(connect()) as conn:
            conn.execute("BEGIN")
            current = self._load(conn, row["request_id"], writing=True)
            if current["cancel_requested"] or time.time() >= current["deadline"]:
                return {"denied": "cancelled_or_expired", "receipt": None}
            outputs, completed = self._read_outputs(conn, current)
            if not set(action["dependencies"]) <= completed:
                raise SidecarProtocolError("domain predecessors have not completed")
        input_digest = digest(outputs)
        status = "completed"
        try:
            value, backend_calls = self._compute(row, action, outputs)
        except (ValueError, ContractValidationError) as error:
            status, value = "failed", {"diagnostic": {"code": "domain_validation_failed", "type": type(error).__name__}}
            backend_calls = getattr(error, "backend_calls", [])
        with transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load(conn, row["request_id"], writing=True)
            if current["cancel_requested"] or time.time() >= current["deadline"]:
                return {"denied": "cancelled_or_expired", "receipt": None}
            if digest(self._read_outputs(conn, current)[0]) != input_digest:
                raise RequestConflictError("domain predecessor output changed")
            children = []
            if status == "completed":
                for name, kind, payload in self._effects(row, action, outputs, value):
                    child = port.execute_in(conn, WriteRequest(kind, port.scope, key + ":effect:" + name,
                                                              payload, row["source_version"]))
                    children.append(child.receipt_id)
            receipt = port.execute_in(conn, WriteRequest(WriteAction.STEP_RECORD, port.scope, key,
                {"run_id": row["run_id"], "node_name": action["id"], "role": "领域节点", "status": status,
                 "input": {"predecessors_digest": input_digest},
                 "output": {"value": value, "backend_calls": backend_calls, "_effect_receipts": children}},
                row["source_version"]))
        return self._receipt_reply(receipt, action)

    def _extra_operation(self, row, kind, message):
        if kind != "verify":
            return super()._extra_operation(row, kind, message)
        with closing(connect()) as conn:
            conn.execute("BEGIN")
            row = self._load(conn, row["request_id"])
            outputs, completed = self._read_outputs(conn, row)
            if "audit" not in completed or "finish" not in outputs:
                raise SidecarProtocolError("domain did not complete its verified effects")
            self.port_type(RunScope(row["case_id"])).verify_sources(conn, self._message(row, outputs["finish"]))
        return {"verification": outputs["finish"], "corpus_version": json.loads(row["binding_json"])["corpus"]}

    def _project_domain(self, conn, row, result):
        if result["status"] == "succeeded":
            outputs, completed = self._read_outputs(conn, row)
            if "audit" not in completed:
                raise SidecarProtocolError("framework success has no corresponding domain receipts")
            finished = outputs["finish"]
            conn.execute("UPDATE agent_runs SET final_answer=?,citations_json=? WHERE id=?",
                         (finished["answer"], canonical(finished["citations"]), row["run_id"]))
