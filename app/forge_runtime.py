"""Framework-backed read-only RuntimeControl (E04).

The one-shot ``forgeharness-readonly`` runtime exposed through the public
control contract. Only ``start()`` is applicable; ``inspect``/``resume``/
``status`` have no durable run trace in the source business database (the
experiment writes only into a private snapshot), so they are rejected
explicitly instead of fabricating results.

Capabilities are declared honestly, not inferred:

- ``idempotent_start`` / ``persistent_request_mapping`` are **False**: this
  engineering runtime keeps no durable request association, so a caller must
  treat a repeated request_id as a new experiment. The full runtime (M4/E13)
  provides the durable mapping and the idempotency guarantees.
- no resume, no background start, no budget enforcement, no persistent event
  projection, no model or remote-embedding overrides.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .forge_bridge import SidecarProtocolError, run_framework
from .forge_governance import OfflineModelTest
from .forge_readonly import ReadonlyCaseSession
from .runtime_ports import (
    DEFAULT_TENANT_ID,
    EXECUTION_MODE_SYNC,
    RUNTIME_PROTOCOL_VERSION,
    ContractValidationError,
    ResumeNotSupportedError,
    RunError,
    RunHandle,
    RunRequest,
    RunResult,
    RuntimeCapabilities,
    RunTrace,
    UnsupportedCapabilityError,
    UnsupportedExecutionModeError,
)

FRAMEWORK_READONLY_RUNTIME = "forgeharness-readonly"

FRAMEWORK_READONLY_CAPABILITIES = RuntimeCapabilities(
    protocol_version=RUNTIME_PROTOCOL_VERSION,
    runtimes=frozenset({FRAMEWORK_READONLY_RUNTIME}),
    start_modes=frozenset({EXECUTION_MODE_SYNC}),
    resumable_runtimes=frozenset(),
    background_start=False,
    budget_enforcement=False,
    event_projection=False,
    idempotent_start=False,
    persistent_request_mapping=False,
    supported_tenants=frozenset({DEFAULT_TENANT_ID}),
)


def resolve_framework_python(explicit: str | Path | None = None) -> Path:
    """Locate the separately installed Python 3.12 framework interpreter.

    The interpreter is trusted deployment configuration (see
    docs/runbook/quality.md); its absence is an explicit capability failure,
    never a silent fallback to the host interpreter.
    """
    raw = str(explicit).strip() if explicit is not None else os.getenv("LAW_REVIEW_FORGE_PYTHON", "").strip()
    if not raw:
        raise UnsupportedCapabilityError(
            "framework interpreter is not configured; set LAW_REVIEW_FORGE_PYTHON to the "
            "separately installed Python 3.12 environment (scripts/setup_framework.py)"
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        raise UnsupportedCapabilityError(f"framework interpreter does not exist: {path}")
    return path


class FrameworkReadonlyControl:
    """RuntimeControl over the one-shot framework read-only runtime."""

    def __init__(
        self,
        *,
        framework_python: str | Path | None = None,
        timeout_seconds: float = 120.0,
        compare_native: bool = True,
        model_test: OfflineModelTest | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 600:
            raise ContractValidationError("timeout_seconds must be in (0, 600]")
        self.framework_python = framework_python
        self.timeout_seconds = timeout_seconds
        self.compare_native = compare_native
        if model_test is not None:
            model_test.validate()
        self.model_test = model_test

    def capabilities(self) -> RuntimeCapabilities:
        return FRAMEWORK_READONLY_CAPABILITIES

    # -- start --------------------------------------------------------------

    def start(self, request: RunRequest) -> RunHandle:
        request.validate()
        if request.runtime != FRAMEWORK_READONLY_RUNTIME:
            raise ContractValidationError(
                f"runtime {request.runtime!r} is not served by the framework read-only control "
                f"({FRAMEWORK_READONLY_RUNTIME!r} only)"
            )
        if request.execution_mode != EXECUTION_MODE_SYNC:
            raise UnsupportedExecutionModeError(
                "the read-only runtime is one-shot and synchronous only"
            )
        if request.budget is not None:
            raise UnsupportedCapabilityError(
                "budget enforcement is not implemented for the read-only runtime"
            )
        if request.model.model is not None or request.model.timeout_seconds is not None:
            raise UnsupportedCapabilityError(
                "per-request model overrides are not implemented for the read-only runtime"
            )
        if request.model.use_llm:
            raise UnsupportedCapabilityError("the read-only runtime serves offline requests only (use_llm=False)")
        if request.use_remote_embeddings:
            raise UnsupportedCapabilityError(
                "remote embedding backends are disabled for the read-only runtime"
            )
        if request.persist_memory:
            raise UnsupportedCapabilityError(
                "memory persistence is not implemented for the read-only runtime"
            )

        started = time.perf_counter()
        try:
            with ReadonlyCaseSession(request) as session:
                # Authorization/identity are checked while constructing the
                # session; the interpreter is trusted deployment configuration
                # and is resolved only after the caller is authorized.
                python = resolve_framework_python(self.framework_python)
                payload = run_framework(
                    session,
                    python,
                    timeout_seconds=self.timeout_seconds,
                    compare_native=self.compare_native,
                    model_test=self.model_test,
                )
        except ValueError as exc:
            # Session construction/validation failures are contract violations.
            raise ContractValidationError(str(exc)) from exc
        except (SidecarProtocolError, TimeoutError, OSError) as exc:
            # The framework outcome is unknown. Nothing was written to the
            # source database (read-only snapshot only), but the contract must
            # not report this as success or failure.
            return RunHandle(
                request_id=request.request_id,
                status="unknown",
                started_new=False,
                needs_reconciliation=True,
                error=RunError(
                    code="framework_outcome_unknown",
                    message="框架只读运行结果未知；源库无写入，请按对账流程复核",
                    runtime=FRAMEWORK_READONLY_RUNTIME,
                    resumable=False,
                    diagnostic={"error_type": type(exc).__name__},
                ),
            )
        total_ms = round((time.perf_counter() - started) * 1000)

        status = str(payload.get("status") or "failed")
        result = payload.get("result") or {}
        trace = payload.get("framework_trace") or []
        legacy: dict[str, Any] = {
            "run_id": None,  # no business run row: the experiment writes a private snapshot only
            "answer": str(result.get("answer") or ""),
            "route": FRAMEWORK_READONLY_RUNTIME,
            "plan": [],
            "steps": [],
            "citations": list(result.get("citations") or []),
            "retrieval_metrics": {},
            "llm_used": False,
            "llm_model": "",
            "fallback_reason": None,
            "validation": result.get("validation"),
            "citation_check": "strict_quotes_verified" if status == "succeeded" else "",
            "runtime": FRAMEWORK_READONLY_RUNTIME,
            "resumable": False,
            "resume_count": 0,
            "checkpoint_thread_id": "",
            "total_ms": total_ms,
            "memory_persisted": bool(result.get("memory_persisted", False)),
            # governance facts for the saved report / reconciliation
            "strict_answer": str(result.get("strict_answer") or ""),
            "source_identity": str(payload.get("source_identity") or ""),
            "framework_version": str(payload.get("framework_version") or ""),
            "wheel_sha256": str(payload.get("wheel_sha256") or ""),
            "tool_calls": payload.get("tool_calls"),
            "framework_trace_valid": True,  # run_framework raises when the trace chain fails
            "framework_trace_events": len(trace) if isinstance(trace, list) else 0,
            "framework_tool_evidence": len(payload.get("framework_tool_evidence") or []),
            "framework_messages": len(payload.get("framework_messages") or []),
            "port_access_log": list(payload.get("access_log") or []),
            "comparison": payload.get("comparison"),
            "statutory_sources": list(result.get("statutory_sources") or []),
            "source_contract_version": result.get("source_contract_version"),
            "answer_projection": result.get("answer_projection"),
            "backend_calls": list(payload.get("backend_calls") or []),
            "framework_usage": payload.get("framework_usage"),
            "offline_model_test": bool(payload.get("offline_model_test")),
            "model_backend": payload.get("model_backend", "disabled"),
            "model_quality_assessed": False,
            "business_qualified": False,
            "source_business_writes": int(payload.get("source_business_writes") or 0),
        }
        run_result = RunResult.from_legacy(legacy)
        if status == "succeeded":
            return RunHandle(
                request_id=request.request_id,
                status="completed",
                started_new=True,
                result=run_result,
            )
        framework_error = payload.get("error")
        return RunHandle(
            request_id=request.request_id,
            status="failed",
            started_new=True,
            result=run_result,
            error=RunError(
                code="framework_readonly_failed",
                message="框架只读运行未通过（严格引文校验或节点执行失败）；源库无写入",
                runtime=FRAMEWORK_READONLY_RUNTIME,
                resumable=False,
                diagnostic={"framework_error": framework_error} if framework_error else None,
            ),
        )

    # -- explicitly unsupported control methods -----------------------------

    def inspect(self, run_id: int) -> RunTrace:
        raise UnsupportedCapabilityError(
            "the read-only runtime keeps no durable run trace in the source database; "
            "use the returned RunResult and the saved experiment report"
        )

    def resume(self, run_id: int) -> RunHandle:
        raise ResumeNotSupportedError("the read-only runtime is one-shot and cannot be resumed")

    def status(self, job_id: str) -> Any:
        raise UnsupportedCapabilityError(
            "the read-only runtime has no background jobs; status() is not applicable"
        )


def framework_readonly_control(
    *, framework_python: str | Path | None = None,
    timeout_seconds: float = 120.0,
    compare_native: bool = True,
    model_test: OfflineModelTest | None = None,
) -> FrameworkReadonlyControl:
    return FrameworkReadonlyControl(
        framework_python=framework_python,
        timeout_seconds=timeout_seconds,
        compare_native=compare_native,
        model_test=model_test,
    )
