"""Versioned runtime contracts for the ForgeHarness migration (M1 freeze).

This module is the single frozen definition of the public run interface named
in docs/architecture/forgeharness-migration-plan-20261021.md §4.1:

- ``RunRequest``       idempotent, versioned request with trusted actor/scope
- ``HostReadPort``     authorized retrieval/page/evidence/memory/statute reads
- ``HostWritePort``    exact business actions with idempotency keys + receipts
- ``RunEvent``         projected, deduplicated, monotonically sequenced events
- ``RunResult``        the existing result semantics, losslessly round-tripped
- ``RuntimeControl``   start/inspect/resume/status with declared capabilities

Rules frozen with this contract (see docs/migration/m1-contracts.md):

1. Boundary purity: this module imports the standard library only. It must
   never import app business modules (agents/db/rag/security/services) and
   must never import ForgeHarness; conformance is enforced by
   tests/test_runtime_ports.py. ``runtime="native"/"langgraph"`` implementations
   live in app/runtime_adapter.py, the future ForgeHarness implementation in a
   separate adapter (M2+).
2. Versioning: ``RUNTIME_PROTOCOL_VERSION`` covers RunRequest/RuntimeControl
   wire semantics; ``RUN_EVENT_VERSION`` and ``RUN_RESULT_VERSION`` version the
   projections independently. Additive optional fields keep the version;
   anything that changes meaning, ordering guarantees or required fields bumps
   it. A mismatched protocol version must be rejected explicitly
   (``ProtocolVersionError``), never silently translated.
3. Idempotency: ``RunRequest.request_id`` is the stable retry key. Retrying a
   request_id must never create a second business run; when the first outcome
   is unknown or unrecoverable the control surface reports that honestly
   (``needs_reconciliation`` / ``RunError``) instead of pretending success or
   failure.
4. Authorization: identity in ``ActorIdentity`` is asserted by the trusted
   host, never by the caller's payload alone. Every control or port call
   re-checks current case authorization at execution time; a granted run does
   not cache access rights.
5. Capability honesty: ``RuntimeCapabilities`` declares what an implementation
   actually supports (start modes, resumable runtimes, budget enforcement).
   Unimplemented behaviour must raise the contract errors defined here, not
   degrade silently.

Field shapes mirror the frozen 0.3.0 behaviour (see m1-contracts.md for the
source anchors); the legacy dict result remains the API currency and
``RunResult.to_legacy()`` reconstructs it exactly.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

# ---------------------------------------------------------------------------
# Versions and frozen constants
# ---------------------------------------------------------------------------

#: v2 (2026-10-21, status-review A1/A2): RunRequest.actor is verified against
#: the live authenticated principal, unsupported tenants are rejected, and
#: idempotent replay requires a matching canonical request digest. v1 rows are
#: handled through the explicit unbound-identity reconciliation state.
RUNTIME_PROTOCOL_VERSION = 2
#: v2 (2026-10-21, status-review A3): events are snapshot projections of
#: mutable step rows, not an append-only log. Identity is
#: (run_id, trace_revision, seq); consumers must re-read on revision change.
RUN_EVENT_VERSION = 2
RUN_RESULT_VERSION = 1

#: Versioned canonicalization schemes (bump on any field/semantic change).
REQUEST_DIGEST_SCHEME = "lexvault-request-binding/1"
TRACE_REVISION_SCHEME = "lexvault-trace-revision/1"

NATIVE_RUNTIME = "native"
LANGGRAPH_RUNTIME = "langgraph"
LEGACY_RUNTIMES = frozenset({NATIVE_RUNTIME, LANGGRAPH_RUNTIME})

DEFAULT_TENANT_ID = "lexvault-local"

#: Same identity grammar as the framework TaskId (^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$).
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: Question bounds mirror ReviewJobRequest (app/review_jobs.py).
MIN_QUESTION_CHARS = 1
MAX_QUESTION_CHARS = 3000

#: Read-port bounds mirror execute_readonly_tool (app/agent_tools.py) and the
#: framework bundle page cap (forgeharness bundle.MAX_PAGE_CHARS).
MAX_QUERY_CHARS = 500
SEARCH_LIMIT_MIN, SEARCH_LIMIT_MAX, SEARCH_LIMIT_DEFAULT = 1, 20, 6
CATALOG_LIMIT_MIN, CATALOG_LIMIT_MAX, CATALOG_LIMIT_DEFAULT = 1, 100, 50
MAX_PAGE_TEXT_CHARS = 20_000

EXECUTION_MODE_SYNC = "sync"
EXECUTION_MODE_BACKGROUND = "background"


def new_request_id() -> str:
    """Generate a protocol-conformant idempotency key (host-side utility)."""
    return uuid.uuid4().hex


def _canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def compute_request_digest(
    request: "RunRequest", *, principal_name: str, effective_author: str
) -> str:
    """Versioned canonical digest binding one request_id to one exact request.

    Frozen rule (status-review A1): only a byte-identical canonical request may
    replay an existing association. The digest covers the case/scope, the
    *verified* principal name, the normalized author, the runtime, execution
    mode, the stripped question and every parameter that affects execution
    (embeddings backend, memory persistence, memory snapshot, model selection
    and budget). Deliberately excluded: ``request_id`` itself (it is the key),
    ``protocol_version`` (checked separately and rejected on mismatch) and the
    descriptive ``app_version`` / ``framework_version`` /
    ``data_snapshot_version`` fields, which do not change execution.

    ``principal_name`` and ``effective_author`` must already be the trusted,
    normalized values resolved by the host (see the adapter); the digest never
    binds raw client-supplied identity claims.
    """
    budget = request.budget
    payload = {
        "scheme": REQUEST_DIGEST_SCHEME,
        "tenant_id": request.scope.tenant_id,
        "case_id": request.scope.case_id,
        "principal_name": principal_name,
        "effective_author": effective_author,
        "runtime": request.runtime,
        "execution_mode": request.execution_mode,
        "question": (request.question or "").strip(),
        "use_remote_embeddings": bool(request.use_remote_embeddings),
        "persist_memory": bool(request.persist_memory),
        "memory_snapshot": list(request.memory_snapshot) if request.memory_snapshot is not None else None,
        "model": {
            "use_llm": bool(request.model.use_llm),
            "model": request.model.model,
            "timeout_seconds": request.model.timeout_seconds,
        },
        "budget": None if budget is None else {
            "max_steps": budget.max_steps,
            "max_tool_calls": budget.max_tool_calls,
            "max_input_tokens": budget.max_input_tokens,
            "max_output_tokens": budget.max_output_tokens,
            "max_wall_seconds": budget.max_wall_seconds,
        },
    }
    return _canonical_sha256(payload)


#: Run-row fields whose change must produce a new trace revision.
_TRACE_RUN_FIELDS = (
    "status", "route", "runtime", "resume_count", "total_ms",
    "finished_at", "final_answer", "checkpoint_thread_id", "retrieval_mode",
)


def compute_trace_revision(run: Mapping[str, Any], steps: Sequence[Mapping[str, Any]]) -> str:
    """Content-derived revision of one run's trace snapshot (status-review A3).

    The legacy runtime mutates step rows in place (a resumed LangGraph run
    rewrites its failed node to completed under the same seq). ``seq`` alone
    is therefore NOT an immutable event identity: consumers must key caches on
    ``(run_id, trace_revision)`` and replace the whole projection whenever the
    revision changes. The revision is deterministic over durable rows, so it
    is stable across processes and restarts; equal rows always yield an equal
    revision, and any status/timing/content change yields a new one.
    """
    step_records = [
        {
            "id": step.get("id"),
            "node_name": step.get("node_name"),
            "agent_role": step.get("agent_role"),
            "status": step.get("status"),
            "latency_ms": step.get("latency_ms"),
            "started_at": step.get("started_at"),
            "finished_at": step.get("finished_at"),
            "input_sha256": _canonical_sha256(step.get("input")),
            "output_sha256": _canonical_sha256(step.get("output")),
        }
        for step in steps
    ]
    payload = {
        "scheme": TRACE_REVISION_SCHEME,
        "run": {key: run.get(key) for key in _TRACE_RUN_FIELDS},
        "citations_sha256": _canonical_sha256(run.get("citations")),
        "steps": step_records,
    }
    return _canonical_sha256(payload)


# ---------------------------------------------------------------------------
# Contract errors (errors are data at the control boundary; the HTTP layer
# maps them, it does not invent semantics)
# ---------------------------------------------------------------------------


class RuntimeContractError(Exception):
    """Base class for all runtime contract violations."""


class ProtocolVersionError(RuntimeContractError):
    """RunRequest.protocol_version is not supported by this implementation."""


class ContractValidationError(RuntimeContractError):
    """A request or port call violates the frozen field contract."""


class UnsupportedExecutionModeError(RuntimeContractError):
    """The implementation does not support the requested execution mode."""


class UnsupportedCapabilityError(RuntimeContractError):
    """The implementation does not support the requested capability.

    E04 addition (additive, no version bump): read-port and control surfaces
    must reject unsupported capabilities explicitly - an unconfigured statute
    corpus, remote embeddings disabled in this deployment, or a control method
    that the one-shot read-only runtime does not provide - instead of
    returning empty results or fabricating success.
    """


class AccessDeniedError(RuntimeContractError):
    """Current authorization does not cover the requested case/resource."""


class IdentityMismatchError(RuntimeContractError):
    """RunRequest.actor does not match the live authenticated principal.

    Identity is asserted by the trusted host auth layer, never by payload; a
    mismatch is rejected instead of silently normalized, while the display
    author name (``user_name``) keeps its existing normalization rules.
    """


class RequestConflictError(RuntimeContractError):
    """The request_id is already bound to a different canonical request.

    Raised without disclosing any detail of the previously bound association
    (no run id, case, status or result), so a reused key cannot be used to
    probe another request's outcome.
    """


class RunNotFoundError(RuntimeContractError):
    """The referenced run does not exist (or is not visible to this caller)."""


class RequestInProgressError(RuntimeContractError):
    """A concurrent start for the same request_id is being executed."""


class ReconciliationRequiredError(RuntimeContractError):
    """A request was accepted but its run association is unknown; fail closed."""


class ResumeNotSupportedError(RuntimeContractError):
    """The run's runtime has no resume capability (declared, not guessed)."""


class ResumeConflictError(RuntimeContractError):
    """Resume preconditions failed (state, ownership, checkpoint identity)."""


# ---------------------------------------------------------------------------
# Identity, scope, configuration records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActorIdentity:
    """Identity asserted by the trusted host process.

    Frozen rule (status-review A2): implementations VERIFY this assertion
    against the live authenticated principal at call time (name, admin and
    local flag must all match) and reject mismatches with
    ``IdentityMismatchError``; privilege fields are never taken from the
    payload alone. The client-facing display name lives in
    ``RunRequest.user_name`` and keeps the host's existing normalization
    (token mode overrides it with the principal name). Cross-process callers
    (M2+) must present host-verified credentials. ``local`` mirrors
    Principal.local: the loopback single-user default.
    """

    name: str
    admin: bool = False
    local: bool = True


@dataclass(frozen=True)
class RunScope:
    """Data scope of one run. LexVault is single-tenant today; ``tenant_id``
    is frozen now so the framework RunContext mapping (M2) needs no contract
    change."""

    case_id: int
    tenant_id: str = DEFAULT_TENANT_ID


@dataclass(frozen=True)
class ModelConfig:
    """Model selection carried by the request. ``None`` means host default
    (LLMConfig / admin settings / environment). The legacy runtime applies
    its existing overrides; the contract only transports intent."""

    use_llm: bool = True
    model: str | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class RunBudget:
    """Budget intent, field-compatible with the framework RunBudget. The
    legacy runtime does not enforce budgets (declared via
    ``RuntimeCapabilities.budget_enforcement=False``); values are recorded and
    forwarded, never silently dropped."""

    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_wall_seconds: float | None = None


@dataclass(frozen=True)
class RunRequest:
    """One idempotent run submission (plan §4.1 RunRequest row)."""

    request_id: str
    scope: RunScope
    actor: ActorIdentity
    question: str
    runtime: str = NATIVE_RUNTIME
    execution_mode: str = EXECUTION_MODE_SYNC
    user_name: str = "本机律师"
    use_remote_embeddings: bool = True
    persist_memory: bool = True
    model: ModelConfig = field(default_factory=ModelConfig)
    budget: RunBudget | None = None
    protocol_version: int = RUNTIME_PROTOCOL_VERSION
    app_version: str = ""
    framework_version: str = ""
    data_snapshot_version: str = ""
    memory_snapshot: tuple[dict[str, Any], ...] | None = None

    def validate(self) -> None:
        if self.protocol_version != RUNTIME_PROTOCOL_VERSION:
            raise ProtocolVersionError(
                f"protocol_version {self.protocol_version} != supported {RUNTIME_PROTOCOL_VERSION}"
            )
        if not REQUEST_ID_PATTERN.match(self.request_id or ""):
            raise ContractValidationError("request_id must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
        if not isinstance(self.scope.case_id, int) or self.scope.case_id < 1:
            raise ContractValidationError("scope.case_id must be a positive integer")
        if not self.scope.tenant_id:
            raise ContractValidationError("scope.tenant_id must not be empty")
        if not self.actor.name:
            raise ContractValidationError("actor.name must not be empty")
        stripped = (self.question or "").strip()
        if not MIN_QUESTION_CHARS <= len(stripped) <= MAX_QUESTION_CHARS:
            raise ContractValidationError(
                f"question must be {MIN_QUESTION_CHARS}..{MAX_QUESTION_CHARS} characters"
            )
        if self.execution_mode not in (EXECUTION_MODE_SYNC, EXECUTION_MODE_BACKGROUND):
            raise ContractValidationError(f"unknown execution_mode {self.execution_mode!r}")


# ---------------------------------------------------------------------------
# Results, events, traces (projections of the frozen legacy semantics)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunError:
    """Failure as data: accurate recovery capability, never a fake terminal."""

    code: str
    message: str
    run_id: int | None = None
    runtime: str = ""
    resumable: bool = False
    checkpoint_thread_id: str = ""
    resume_count: int = 0
    diagnostic: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunResult:
    """Result contract = the existing result semantics (plan §4.1 RunResult).

    Typed fields carry the frozen keys of the 0.3.0 result dict; every other
    legacy key (agent_type, tools_used, memory_hits, mode, user_name,
    checkpoint_size_bytes, conversation_id, future additions) round-trips
    through ``extra`` so ``to_legacy()`` is lossless for both runtimes and for
    stored review_jobs result_json payloads.
    """

    run_id: int | None
    answer: str
    route: str
    plan: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    retrieval_metrics: dict[str, Any]
    llm_used: bool
    llm_model: str
    fallback_reason: str | None
    validation: dict[str, Any] | None
    citation_check: str
    runtime: str
    resumable: bool
    resume_count: int
    checkpoint_thread_id: str
    total_ms: int
    memory_persisted: bool
    result_version: int = RUN_RESULT_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    _TYPED_KEYS = frozenset({
        "run_id", "answer", "route", "plan", "steps", "citations",
        "retrieval_metrics", "llm_used", "llm_model", "fallback_reason",
        "validation", "citation_check", "runtime", "resumable", "resume_count",
        "checkpoint_thread_id", "total_ms", "memory_persisted", "result_version",
    })

    @classmethod
    def from_legacy(cls, payload: dict[str, Any]) -> "RunResult":
        extra = {k: v for k, v in payload.items() if k not in cls._TYPED_KEYS}
        return cls(
            run_id=payload.get("run_id"),
            answer=payload.get("answer", ""),
            route=payload.get("route", ""),
            plan=list(payload.get("plan") or []),
            steps=list(payload.get("steps") or []),
            citations=list(payload.get("citations") or []),
            retrieval_metrics=dict(payload.get("retrieval_metrics") or {}),
            llm_used=bool(payload.get("llm_used", False)),
            llm_model=str(payload.get("llm_model", "")),
            fallback_reason=payload.get("fallback_reason"),
            validation=payload.get("validation"),
            citation_check=str(payload.get("citation_check", "")),
            runtime=str(payload.get("runtime", "")),
            resumable=bool(payload.get("resumable", False)),
            resume_count=int(payload.get("resume_count", 0) or 0),
            checkpoint_thread_id=str(payload.get("checkpoint_thread_id", "") or ""),
            total_ms=int(payload.get("total_ms", 0) or 0),
            memory_persisted=bool(payload.get("memory_persisted", False)),
            extra=extra,
        )

    def to_legacy(self) -> dict[str, Any]:
        payload: dict[str, Any] = dict(self.extra)
        payload.update({
            "run_id": self.run_id,
            "answer": self.answer,
            "route": self.route,
            "plan": self.plan,
            "steps": self.steps,
            "citations": self.citations,
            "retrieval_metrics": self.retrieval_metrics,
            "llm_used": self.llm_used,
            "llm_model": self.llm_model,
            "fallback_reason": self.fallback_reason,
            "validation": self.validation,
            "citation_check": self.citation_check,
            "runtime": self.runtime,
            "resumable": self.resumable,
            "resume_count": self.resume_count,
            "checkpoint_thread_id": self.checkpoint_thread_id,
            "total_ms": self.total_ms,
            "memory_persisted": self.memory_persisted,
        })
        return payload


@dataclass(frozen=True)
class RunEvent:
    """Snapshot-projection event of one run/step row (plan §4.1 RunEvent).

    Semantics frozen at event version 2 (status-review A3): legacy step rows
    are MUTABLE - a resumed LangGraph run rewrites its failed node in place -
    so ``seq`` (monotonic per snapshot, ordered by agent_steps.id ASC) is NOT
    an immutable append-only identity. Event identity is valid only within one
    ``RunTrace.trace_revision``: consumers must key caches on
    ``(run_id, trace_revision, seq)``, replace the whole projection when the
    revision changes, and never treat an earlier snapshot's events as final.
    Re-reading the same revision is idempotent and deterministic across
    processes/restarts. The framework side (M2+) provides append-only durable
    events; the adapter documents the mapping between the two semantics.
    ``node_id`` is the stable node name; ``usage`` is ``None`` while the
    legacy runtime does not attribute tokens per node (unknown stays unknown).
    """

    run_id: int
    seq: int
    node_id: str
    status: str
    role: str = ""
    started_at: str = ""
    finished_at: str = ""
    latency_ms: int = 0
    usage: dict[str, Any] | None = None
    diagnostic: dict[str, Any] | None = None
    event_version: int = RUN_EVENT_VERSION

    @classmethod
    def from_legacy_step(cls, run_id: int, seq: int, step: dict[str, Any]) -> "RunEvent":
        output = step.get("output") or {}
        diagnostic = output.get("diagnostic") if isinstance(output, dict) else None
        return cls(
            run_id=run_id,
            seq=seq,
            node_id=str(step.get("node_name", "")),
            status=str(step.get("status", "")),
            role=str(step.get("agent_role", "")),
            started_at=str(step.get("started_at", "") or ""),
            finished_at=str(step.get("finished_at", "") or ""),
            latency_ms=int(step.get("latency_ms", 0) or 0),
            diagnostic=diagnostic if isinstance(diagnostic, dict) else None,
        )


@dataclass(frozen=True)
class RunTrace:
    """inspect() projection: durable run row + ordered events. ``raw_steps``
    keeps the exact legacy step dicts so existing UI payloads stay byte-
    compatible while events carry the versioned projection.

    ``trace_revision`` is the snapshot revision (see compute_trace_revision):
    it changes whenever the run row or any step row changes (including
    in-place resume updates), and is stable for identical durable state.
    Consumers update by revision instead of assuming append-only events."""

    run_id: int
    case_id: int
    question: str
    route: str
    status: str
    runtime: str
    resumable: bool
    checkpoint_thread_id: str
    resume_count: int
    created_at: str
    finished_at: str
    total_ms: int
    final_answer: str
    citations: list[dict[str, Any]]
    events: tuple[RunEvent, ...]
    raw_steps: list[dict[str, Any]]
    retrieval_mode: str = ""
    trace_revision: str = ""


@dataclass(frozen=True)
class RunHandle:
    """start()/resume() outcome. Exactly one of result/error is meaningful per
    status; ``started_new=False`` marks an idempotent replay of an existing
    association, and ``needs_reconciliation=True`` means the outcome of a
    previously accepted request is unknown and must be reconciled (never
    blindly re-run)."""

    request_id: str
    status: str
    run_id: int | None = None
    job_id: str | None = None
    started_new: bool = False
    needs_reconciliation: bool = False
    result: RunResult | None = None
    error: RunError | None = None


@dataclass(frozen=True)
class JobStatus:
    """status() projection of one background job (review_jobs semantics)."""

    job_id: str
    case_id: int
    status: str
    run_id: int | None
    runtime: str
    resumable: bool
    error: dict[str, Any] | None
    result: RunResult | None
    created_at: str
    finished_at: str


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Declared, not inferred. UI and API surfaces must render recovery
    affordances from this plus per-run ``resumable``, never from runtime
    names alone (plan §6 后台与产品). ``supported_tenants`` makes the
    single-tenant boundary explicit: an implementation must reject tenants it
    does not actually isolate (status-review A2)."""

    protocol_version: int
    runtimes: frozenset[str]
    start_modes: frozenset[str]
    resumable_runtimes: frozenset[str]
    background_start: bool
    budget_enforcement: bool
    event_projection: bool
    idempotent_start: bool
    persistent_request_mapping: bool
    supported_tenants: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# RuntimeControl (start / inspect / resume / status)
# ---------------------------------------------------------------------------


class RuntimeControl(Protocol):
    """Public run control surface. The legacy implementation is
    app/runtime_adapter.py::LegacyRuntimeControl; the ForgeHarness-backed
    implementation arrives with M2 (read-only) / M4 (full product path).

    All methods re-check current authorization for the referenced case at
    call time, and replayed associations re-verify authorization of the
    actually mapped run (status-review A1). Errors are raised as
    RuntimeContractError subclasses; the legacy HTTP mapping is:
    AccessDeniedError/RunNotFoundError -> 404,
    IdentityMismatchError -> 403,
    ResumeConflictError/RequestInProgressError/RequestConflictError -> 409,
    ResumeNotSupportedError -> 409 with capability detail,
    ProtocolVersionError/ContractValidationError -> 422,
    UnsupportedExecutionModeError -> 501.
    """

    def capabilities(self) -> RuntimeCapabilities: ...

    def start(self, request: RunRequest) -> RunHandle: ...

    def inspect(self, run_id: int) -> RunTrace: ...

    def resume(self, run_id: int) -> RunHandle: ...

    def status(self, job_id: str) -> JobStatus: ...


# ---------------------------------------------------------------------------
# Host read port (authorized reads; implementation lands with M2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentRef:
    document_id: int
    name: str
    doc_type: str
    mime_type: str
    page_count: int
    status: str
    people: str
    date_range: str
    summary: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: int
    title: str
    category: str
    fact: str
    credibility: str
    status: str
    source_document_id: int | None
    source_page_start: int | None
    source_page_end: int | None
    quote: str
    source_name: str
    created_at: str


@dataclass(frozen=True)
class PageContent:
    """One physical/logical page with citation identity. ``page_no`` is the
    document-local page number; the M2 bundle layer maps (document_id,
    page_no) to framework citation numbers, covering duplicate page numbers
    across documents (plan §4.1). ``text_sha256`` is the content identity used
    by citation verification."""

    document_id: int
    page_no: int
    text: str
    text_sha256: str
    summary: str


@dataclass(frozen=True)
class SearchHit:
    """Retrieval hit with the frozen allowed-field set of the controlled
    search tool (app/agent_tools.py::_search). ``extra`` keeps forward-
    compatible diagnostics without breaking the identity fields."""

    page_id: int
    page_no: int
    document_id: int
    name: str
    doc_type: str
    quote: str
    text: str
    rank: int
    retrieval_explain: str
    channels: tuple[str, ...]
    is_neighbor: bool
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    metrics: dict[str, Any]


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: int
    content: str
    importance: float
    source_run_id: int | None
    kind: str
    created_at: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StatuteHit:
    law_name: str
    article_no: str
    text: str
    score: float
    corpus_version: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GapRecord:
    gap_id: int
    run_id: int | None
    gap_type: str
    severity: str
    description: str
    suggestion: str
    affected_evidence_ids: tuple[int, ...]
    created_at: str


class HostReadPort(Protocol):
    """Authorized read surface exposed to a runtime process (plan §4.1).

    Frozen guarantees: every method re-checks current case authorization per
    access (a started run does not cache rights; revocation takes effect on
    the next call); queries are bounded by the constants above; returned
    content is untrusted case data, never instructions; implementations log
    access through the host's audit trail (M2: isolated experiment storage,
    M4: the accepted business audit)."""

    def list_documents(self, scope: RunScope, *, limit: int = CATALOG_LIMIT_DEFAULT) -> tuple[DocumentRef, ...]: ...

    def list_evidence(self, scope: RunScope, *, limit: int = CATALOG_LIMIT_DEFAULT) -> tuple[EvidenceRef, ...]: ...

    def search(self, scope: RunScope, query: str, *, limit: int = SEARCH_LIMIT_DEFAULT,
               use_remote_embeddings: bool = True) -> SearchResult: ...

    def read_page(self, scope: RunScope, document_id: int, page_no: int) -> PageContent: ...

    def recall_memories(self, scope: RunScope, query: str, *, limit: int = 3,
                        use_remote_embeddings: bool = True) -> tuple[MemoryRecord, ...]: ...

    def search_statutes(self, scope: RunScope, query: str, *, limit: int = SEARCH_LIMIT_DEFAULT) -> tuple[StatuteHit, ...]: ...

    def saved_gap_analysis(self, scope: RunScope, *, severity: str | None = None,
                           limit: int = CATALOG_LIMIT_DEFAULT) -> tuple[GapRecord, ...]: ...


# ---------------------------------------------------------------------------
# Host write port (exact business actions; implementation lands with M2/M4)
# ---------------------------------------------------------------------------


class WriteAction(str, Enum):
    """Exhaustive M1 action set. The framework may only request these exact
    actions; the host owns transaction, permission check and unique-key
    enforcement for each (plan §4.2). New actions require a contract version
    note, never ad-hoc payloads."""

    RUN_RECORD = "run_record"
    STEP_RECORD = "step_record"
    GAP_DETECTION = "gap_detection"
    MEMORY_DRAFT = "memory_draft"
    CONVERSATION_MESSAGE = "conversation_message"
    AUDIT_ENTRY = "audit_entry"


@dataclass(frozen=True)
class WriteRequest:
    """One exact business write. ``idempotency_key`` is stable per logical
    write (e.g. run id + node name for steps, run id + gap fingerprint for
    gaps, source_run_id for memory drafts); ``source_version`` carries the
    data snapshot/resource version the write was produced against so the host
    can refuse stale writes."""

    action: WriteAction
    scope: RunScope
    idempotency_key: str
    payload: dict[str, Any]
    source_version: str = ""


@dataclass(frozen=True)
class WriteReceipt:
    """Execution receipt. ``duplicate=True`` means the idempotency key was
    already applied and no second side effect happened. ``status`` is one of
    ``applied`` / ``duplicate`` / ``rejected``; rejection carries a reason in
    ``detail`` without leaking case content."""

    receipt_id: str
    action: WriteAction
    idempotency_key: str
    accepted: bool
    duplicate: bool
    status: str
    detail: str = ""


class HostWritePort(Protocol):
    """Authorized write surface. Unknown outcomes (timeout after side effect)
    must be resolved by ``query_receipt`` before any retry; blind replay is
    forbidden (plan §4.2). The host completes each write in its own
    transaction; there is no cross-database atomic commit with framework
    state."""

    def execute(self, request: WriteRequest) -> WriteReceipt: ...

    def query_receipt(self, scope: RunScope, action: WriteAction, idempotency_key: str) -> WriteReceipt | None: ...
