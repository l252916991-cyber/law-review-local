"""Legacy-runtime implementation of the public RuntimeControl contract (M1).

Plan reference: docs/architecture/forgeharness-migration-plan-20261021.md §5 M1
("先用旧运行时实现兼容接口") and docs/migration/m1-contracts.md.

This adapter gives the existing native DAG and LangGraph runtimes the frozen
public interface without changing any legacy behaviour:

- start()  executes the synchronous coordinator path (same code path as
  POST /api/cases/{id}/agent-chat) behind a durable idempotency mapping
  (runtime_requests, schema v14 + v15 binding columns): only a byte-identical
  canonical request (versioned digest over scope, verified principal,
  normalized author, runtime, question and execution-affecting parameters)
  may replay an association; a reused request_id bound to a different request
  is rejected without disclosing the old association; retrying one identical
  request never creates a second business run; unknown associations fail
  closed instead of re-running.
- Identity is verified, not trusted: RunRequest.actor must match the live
  authenticated principal (name/admin/local), the display author name keeps
  the existing actor_name() normalization, and unsupported tenants are
  rejected explicitly (single-tenant host).
- inspect() projects agent_runs/agent_steps into RunTrace + RunEvents with a
  content-derived trace_revision: legacy step rows are mutable across resume,
  so consumers key on (run_id, trace_revision, seq), never on seq alone.
- resume() wraps the existing LangGraph recovery with capability checks.
- status() projects review_jobs rows exactly like the polling route.

Deliberate M1 boundaries (declared via capabilities, not silently missing):
- background start stays on the existing agent-jobs API until the M4 rewiring;
- budgets/model overrides are transported but not enforced by the legacy
  runtime (host configuration still owns model selection);
- the adapter writes no audit_log entries: audit parity belongs to the API
  entry points (existing routes today, M4 wiring tomorrow), matching the
  §4.2 ownership split where the host owns business writes;
- run/step/gap/memory writes keep happening inside the coordinators exactly
  as before - this adapter adds association rows, never a second state store.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import closing
from functools import wraps
from typing import Any

from fastapi import HTTPException

from . import agents, langgraph_agents
from .db import connect, db_scope, get_db_path, now, transaction
from .runtime_ports import (
    DEFAULT_TENANT_ID,
    EXECUTION_MODE_SYNC,
    LANGGRAPH_RUNTIME,
    LEGACY_RUNTIMES,
    RUNTIME_PROTOCOL_VERSION,
    AccessDeniedError,
    ActorIdentity,
    ContractValidationError,
    IdentityMismatchError,
    JobStatus,
    ProtocolVersionError,
    ReconciliationRequiredError,
    RequestConflictError,
    RequestInProgressError,
    ResumeConflictError,
    ResumeNotSupportedError,
    RunError,
    RunEvent,
    RunHandle,
    RunNotFoundError,
    RunRequest,
    RunResult,
    RuntimeCapabilities,
    RuntimeControl,
    RunTrace,
    UnsupportedExecutionModeError,
    compute_request_digest,
    compute_trace_revision,
)
from .security import actor_name, current_principal, require_case_access, require_permission

logger = logging.getLogger(__name__)

LEGACY_CAPABILITIES = RuntimeCapabilities(
    protocol_version=RUNTIME_PROTOCOL_VERSION,
    runtimes=LEGACY_RUNTIMES,
    start_modes=frozenset({EXECUTION_MODE_SYNC}),
    resumable_runtimes=frozenset({LANGGRAPH_RUNTIME}),
    background_start=False,
    budget_enforcement=False,
    event_projection=True,
    idempotent_start=True,
    persistent_request_mapping=True,
    supported_tenants=frozenset({DEFAULT_TENANT_ID}),
)

_SYNC_FAILURE_MESSAGE = "同步阅卷执行失败，请查看节点轨迹或恢复可用的 LangGraph 运行"


def current_actor_identity() -> ActorIdentity:
    """Build the contract actor from the live authenticated principal.

    Host entry points (and tests) use this so RunRequest.actor always mirrors
    the trusted auth context instead of a hand-written claim.
    """
    try:
        principal = current_principal()
    except HTTPException as exc:
        raise AccessDeniedError(str(exc.detail)) from exc
    return ActorIdentity(name=principal.name, admin=principal.admin, local=principal.local)


def _with_database_scope(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def scoped(*args: Any, **kwargs: Any) -> Any:
        with db_scope(get_db_path()):
            return method(*args, **kwargs)
    return scoped


class LegacyRuntimeControl:
    """RuntimeControl over the frozen 0.3.0 runtimes and tables."""

    def capabilities(self) -> RuntimeCapabilities:
        return LEGACY_CAPABILITIES

    # -- authorization and identity -----------------------------------------

    def _authorize(self, case_id: int) -> None:
        """Re-check current authorization at call time (never cached)."""
        try:
            require_permission("view")
            require_case_access(case_id)
        except HTTPException as exc:
            raise AccessDeniedError(f"case {case_id}: {exc.detail}") from exc

    def _resolve_identity(self, request: RunRequest) -> tuple[str, str]:
        """Verify the asserted actor against the live principal (A2).

        Returns (trusted principal name, normalized effective author). The
        actor assertion must match the authenticated principal exactly; the
        display name keeps the host's actor_name() normalization (token mode
        overrides the client-provided name, local mode preserves it).
        """
        try:
            principal = current_principal()
        except HTTPException as exc:
            raise AccessDeniedError(str(exc.detail)) from exc
        if (
            request.actor.name != principal.name
            or bool(request.actor.admin) != bool(principal.admin)
            or bool(request.actor.local) != bool(principal.local)
        ):
            raise IdentityMismatchError(
                "RunRequest.actor does not match the authenticated principal; "
                "identity is asserted by the host auth layer, not by the payload"
            )
        return principal.name, actor_name(request.user_name)

    # -- start --------------------------------------------------------------

    @_with_database_scope
    def start(self, request: RunRequest) -> RunHandle:
        request.validate()
        if request.execution_mode != EXECUTION_MODE_SYNC:
            raise UnsupportedExecutionModeError(
                "legacy control serves sync starts only; background submission stays on the "
                "agent-jobs API until the M4 rewiring"
            )
        if request.runtime not in LEGACY_RUNTIMES:
            raise ContractValidationError(
                f"runtime {request.runtime!r} is not served by the legacy control "
                "(native/langgraph only; the ForgeHarness runtime arrives with M2)"
            )
        if request.scope.tenant_id not in LEGACY_CAPABILITIES.supported_tenants:
            # Explicit rejection beats accepting a tenant field this host does
            # not actually isolate (status-review A2).
            raise ContractValidationError(
                f"tenant_id {request.scope.tenant_id!r} is not supported by this host "
                f"(single-tenant deployment: {sorted(LEGACY_CAPABILITIES.supported_tenants)})"
            )
        principal_name, effective_author = self._resolve_identity(request)
        self._authorize(request.scope.case_id)
        digest = compute_request_digest(
            request, principal_name=principal_name, effective_author=effective_author
        )

        if not self._claim_request(request, digest, principal_name):
            return self._replay(request, digest)

        linked_run_id: list[int] = []

        def link_run(run_id: int) -> None:
            linked_run_id.append(run_id)
            with transaction() as conn:
                conn.execute(
                    "UPDATE runtime_requests SET run_id=? WHERE request_id=?",
                    (run_id, request.request_id),
                )

        execution_entered = False
        try:
            coordinator = self._coordinator(request, link_run)
            execution_entered = True
            payload = coordinator.process_query(
                request.question.strip(),
                effective_author,
                request.model.use_llm,
                persist_memory=request.persist_memory,
                memory_snapshot=list(request.memory_snapshot) if request.memory_snapshot is not None else None,
            )
        except langgraph_agents.LangGraphRunError as exc:
            run_id = linked_run_id[-1] if linked_run_id else exc.run_id
            return RunHandle(
                request_id=request.request_id,
                status="failed",
                run_id=run_id,
                started_new=True,
                error=RunError(
                    code=str(exc),
                    message=_SYNC_FAILURE_MESSAGE,
                    run_id=exc.run_id,
                    runtime=exc.runtime,
                    resumable=exc.resumable,
                    checkpoint_thread_id=exc.checkpoint_thread_id,
                    resume_count=exc.resume_count,
                ),
            )
        except Exception as exc:
            diagnostic = agents.failure_diagnostic(exc, "runtime_adapter")
            logger.warning(
                "legacy_runtime_start_failed runtime=%s request_id=%s error_type=%s",
                request.runtime, request.request_id, type(exc).__name__,
            )
            uncertain = execution_entered and not linked_run_id
            if not execution_entered:
                # Constructors do not start runs. Once process_query has been
                # entered, missing callback delivery does not prove no commit.
                self._release_claim(request.request_id)
            return RunHandle(
                request_id=request.request_id,
                status="failed",
                run_id=linked_run_id[-1] if linked_run_id else None,
                started_new=True,
                needs_reconciliation=uncertain,
                error=RunError(
                    code=diagnostic["code"],
                    message=_SYNC_FAILURE_MESSAGE,
                    run_id=linked_run_id[-1] if linked_run_id else None,
                    runtime=request.runtime,
                    resumable=False,
                    diagnostic=diagnostic,
                ),
            )
        result = RunResult.from_legacy(payload)
        return RunHandle(
            request_id=request.request_id,
            status="completed",
            run_id=result.run_id if result.run_id is not None else (linked_run_id[-1] if linked_run_id else None),
            started_new=True,
            result=result,
        )

    def _coordinator(self, request: RunRequest, link_run: Callable[[int], None]) -> Any:
        if request.runtime == LANGGRAPH_RUNTIME:
            return langgraph_agents.create_langgraph_coordinator(
                request.scope.case_id, request.use_remote_embeddings, on_run_started=link_run
            )
        return agents.create_coordinator(
            request.scope.case_id, request.use_remote_embeddings, on_run_started=link_run
        )

    def _claim_request(self, request: RunRequest, digest: str, principal_name: str) -> bool:
        """True when this call won the request_id claim and must execute."""
        with transaction() as conn:
            claimed = conn.execute(
                """INSERT OR IGNORE INTO runtime_requests
                   (request_id, case_id, runtime, protocol_version, created_at,
                    request_digest, principal_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (request.request_id, request.scope.case_id, request.runtime,
                 request.protocol_version, now(), digest, principal_name),
            ).rowcount
        return claimed == 1

    def _release_claim(self, request_id: str) -> None:
        with transaction() as conn:
            conn.execute(
                "DELETE FROM runtime_requests WHERE request_id=? AND run_id IS NULL",
                (request_id,),
            )

    def _replay(self, request: RunRequest, digest: str) -> RunHandle:
        """Idempotent retry of the SAME canonical request only (A1).

        - No stored digest (pre-v15 record): explicit unbound-identity
          reconciliation state; never a successful replay of a new request.
        - Different digest: reject as conflict without disclosing any detail
          of the bound association (no run id, case, status or result).
        - Equal digest: report the existing association after re-verifying
          authorization of the ACTUAL mapped run's case.
        """
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT run_id, request_digest FROM runtime_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - lost race with a deletion; fail closed
            raise RequestInProgressError(f"request {request.request_id} is being claimed concurrently")
        stored_digest = str(row["request_digest"] or "")
        if not stored_digest:
            raise ReconciliationRequiredError(
                f"request {request.request_id} predates request-identity binding; "
                "its association cannot be proven identical and requires reconciliation "
                "before any replay"
            )
        if stored_digest != digest:
            raise RequestConflictError(
                f"request_id {request.request_id} is already bound to a different request; "
                "a request_id must not be reused across cases, principals, questions, "
                "runtimes or execution parameters"
            )
        run_id = row["run_id"]
        if run_id is None:
            # Accepted earlier but the run association never landed (crash
            # window between the run insert and the mapping update). Fail
            # closed: no second business run, explicit reconciliation needed.
            raise ReconciliationRequiredError(
                f"request {request.request_id} was accepted but its run association is unknown; "
                "reconcile via runtime_requests/agent_runs before any retry"
            )
        trace = agents.get_run_trace(int(run_id))
        if not trace:  # pragma: no cover - unreachable while the FK sets run_id
            # NULL on run deletion; kept as fail-closed defense against a
            # manually corrupted mapping instead of inventing a status.
            raise ReconciliationRequiredError(
                f"request {request.request_id} maps to a run that no longer exists; reconciliation required"
            )
        # Re-verify authorization against the case of the ACTUAL mapped run,
        # not just the requested scope (defense in depth against a corrupted
        # or stale mapping).
        self._authorize(int(trace["case_id"]))
        if (int(trace["case_id"]) != request.scope.case_id
                or trace.get("runtime") != request.runtime
                or str(trace.get("question", "")).strip() != request.question.strip()):
            raise ReconciliationRequiredError("stored request association is inconsistent; reconciliation required")
        return RunHandle(
            request_id=request.request_id,
            status=str(trace.get("status", "running")),
            run_id=int(run_id),
            started_new=False,
        )

    # -- inspect ------------------------------------------------------------

    @_with_database_scope
    def inspect(self, run_id: int) -> RunTrace:
        trace = agents.get_run_trace(run_id)
        if not trace:
            raise RunNotFoundError(f"run {run_id} does not exist")
        self._authorize(int(trace["case_id"]))
        steps = list(trace.get("steps") or [])
        events = tuple(
            RunEvent.from_legacy_step(run_id, seq, step) for seq, step in enumerate(steps, 1)
        )
        revision = compute_trace_revision(trace, steps)
        return RunTrace(
            run_id=run_id,
            case_id=int(trace["case_id"]),
            question=str(trace.get("question", "")),
            route=str(trace.get("route", "")),
            status=str(trace.get("status", "")),
            runtime=str(trace.get("runtime", "")),
            resumable=bool(trace.get("resumable", False)),
            checkpoint_thread_id=str(trace.get("checkpoint_thread_id", "") or ""),
            resume_count=int(trace.get("resume_count", 0) or 0),
            created_at=str(trace.get("created_at", "") or ""),
            finished_at=str(trace.get("finished_at", "") or ""),
            total_ms=int(trace.get("total_ms", 0) or 0),
            final_answer=str(trace.get("final_answer", "") or ""),
            citations=list(trace.get("citations") or []),
            events=events,
            raw_steps=steps,
            retrieval_mode=str(trace.get("retrieval_mode", "") or ""),
            trace_revision=revision,
        )

    # -- resume -------------------------------------------------------------

    @_with_database_scope
    def resume(self, run_id: int) -> RunHandle:
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT case_id, runtime FROM agent_runs WHERE id=?", (run_id,)
            ).fetchone()
            mapping = conn.execute(
                "SELECT request_id FROM runtime_requests WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            raise RunNotFoundError(f"run {run_id} does not exist")
        self._authorize(int(row["case_id"]))
        request_id = str(mapping["request_id"]) if mapping else ""
        if row["runtime"] != LANGGRAPH_RUNTIME:
            raise ResumeNotSupportedError("原生 DAG 运行不支持 checkpoint 恢复")
        try:
            payload = langgraph_agents.resume_langgraph_run(run_id)
        except KeyError as exc:
            raise RunNotFoundError(str(exc)) from exc
        except langgraph_agents.LangGraphRunError as exc:
            return RunHandle(
                request_id=request_id,
                status="failed",
                run_id=run_id,
                started_new=False,
                error=RunError(
                    code=str(exc),
                    message=_SYNC_FAILURE_MESSAGE,
                    run_id=exc.run_id,
                    runtime=exc.runtime,
                    resumable=exc.resumable,
                    checkpoint_thread_id=exc.checkpoint_thread_id,
                    resume_count=exc.resume_count,
                ),
            )
        except ValueError as exc:
            raise ResumeConflictError(str(exc)) from exc
        return RunHandle(
            request_id=request_id,
            status="completed",
            run_id=run_id,
            started_new=False,
            result=RunResult.from_legacy(payload),
        )

    # -- status -------------------------------------------------------------

    @_with_database_scope
    def status(self, job_id: str) -> JobStatus:
        with closing(connect()) as conn:
            row = conn.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise RunNotFoundError(f"后台任务 {job_id} 不存在")
        self._authorize(int(row["case_id"]))
        trace: dict[str, Any] = agents.get_run_trace(row["run_id"]) if row["run_id"] else {}
        if trace:
            self._authorize(int(trace["case_id"]))
            if int(trace["case_id"]) != int(row["case_id"]):
                raise ReconciliationRequiredError("job association is inconsistent; reconciliation required")
        # The schema stores '{}' for "nothing recorded"; normalize to None so
        # contract consumers never mistake an empty object for a real payload.
        error = json.loads(row["error_json"]) if row["error_json"] else None
        error = error or None
        if row["status"] == "interrupted" and not error:
            error = {"type": "Interrupted", "message": "服务重启中断了任务；存在 LangGraph 检查点时可继续执行"}
        result_payload = json.loads(row["result_json"]) if row["result_json"] else None
        result_payload = result_payload or None
        runtime = trace.get("runtime") or (
            "langgraph" if json.loads(row["request_json"]).get("mode") == "langgraph" else "native"
        )
        return JobStatus(
            job_id=job_id,
            case_id=int(row["case_id"]),
            status=str(row["status"]),
            run_id=row["run_id"],
            runtime=str(runtime),
            resumable=bool(trace.get("resumable", False)),
            error=error,
            result=RunResult.from_legacy(result_payload) if result_payload else None,
            created_at=str(row["created_at"] or ""),
            finished_at=str(row["finished_at"] or ""),
        )


def legacy_runtime_control() -> RuntimeControl:
    """Factory used by tests and (from M4) the API layer."""
    return LegacyRuntimeControl()


__all__ = [
    "LEGACY_CAPABILITIES",
    "LegacyRuntimeControl",
    "current_actor_identity",
    "legacy_runtime_control",
    # re-exported so callers depend on one module surface
    "AccessDeniedError",
    "ActorIdentity",
    "IdentityMismatchError",
    "ProtocolVersionError",
    "ReconciliationRequiredError",
    "RequestConflictError",
    "RequestInProgressError",
    "ResumeConflictError",
    "ResumeNotSupportedError",
    "RunNotFoundError",
    "RunRequest",
    "UnsupportedExecutionModeError",
]
