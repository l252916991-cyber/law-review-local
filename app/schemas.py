"""Declared response shapes for the core read endpoints.

These models document what the frontend relies on and feed the generated
TypeScript types (``frontend/src/lib/schema.d.ts``). They never reshape a
response: every model allows extra keys, so a field a handler returns but a
model does not list still reaches the client unchanged, and declared fields
match the database columns (nullable where the column is nullable). Nested
structures whose shape depends on the runtime path (retrieval metrics,
provenance, agent step payloads, stored historical citations) stay
``dict[str, Any]`` instead of pretending to be fixed.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow")


# --- cases -----------------------------------------------------------------------

class CaseRow(ApiModel):
    id: int
    title: str
    case_no: str
    case_type: str
    client_name: str
    status: str
    description: str
    created_at: str
    updated_at: str
    lifecycle_status: str
    archived_at: str | None
    archived_by: str | None
    trashed_at: str | None
    purge_after: str | None
    version: int


class CaseSummary(CaseRow):
    document_count: int
    page_count: int
    evidence_count: int


class CaseMetrics(ApiModel):
    documents: int
    pages: int
    evidence: int
    confirmed: int


class CaseDetail(CaseRow):
    metrics: CaseMetrics


# --- documents and pages ---------------------------------------------------------

class DocumentSummary(ApiModel):
    id: int
    case_id: int
    name: str
    stored_path: str
    import_key: str | None
    mime_type: str
    pages: int
    status: str
    doc_type: str
    people: str
    date_range: str
    summary: str
    created_at: str
    updated_at: str
    content_hash: str | None
    version: int


class PageView(ApiModel):
    id: int
    document_id: int
    page_no: int
    text: str
    summary: str
    name: str
    doc_type: str
    document_version: int


class SearchHit(ApiModel):
    page_id: int
    document_id: int
    name: str
    page_no: int
    text: str
    summary: str
    doc_type: str
    people: str
    date_range: str
    score: float
    matches: list[str]
    quote: str


# --- evidence ----------------------------------------------------------------------

class EvidenceItem(ApiModel):
    id: int
    case_id: int
    title: str
    category: str
    fact: str
    credibility: str
    source_document_id: int | None
    source_page_start: int
    source_page_end: int
    quote: str
    status: str
    created_at: str
    approved_by: str | None
    approved_at: str | None
    version: int
    updated_at: str | None
    source_name: str | None


class EvidenceRelation(ApiModel):
    id: int
    case_id: int
    from_evidence_id: int
    to_evidence_id: int
    relation_type: str
    note: str


class EvidenceList(ApiModel):
    evidence: list[EvidenceItem]
    relations: list[EvidenceRelation]


# --- conversations and chat ---------------------------------------------------------

class Citation(ApiModel):
    """A citation card produced in this turn: one-to-one with a prompt context."""

    index: int
    document_id: int
    document_name: str
    page: int
    quote: str
    url: str


class ConversationSummary(ApiModel):
    id: int
    case_id: int
    user_name: str
    title: str
    created_at: str
    archived_at: str | None
    archive_expires_at: int | None
    archived_by: str | None
    message_count: int
    archived: bool


class Message(ApiModel):
    id: int
    conversation_id: int
    role: str
    content: str
    route: str
    created_at: str
    provenance_json: str
    kind: str
    source_run_id: int | None
    # Stored rows span several releases; their citation shape is not migrated.
    citations: list[dict[str, Any]]


class ChatResult(ApiModel):
    conversation_id: int
    message_id: int
    answer: str
    answer_status: str
    clarification: dict[str, Any] | None
    route: str
    semantic_expansion: list[str]
    citations: list[Citation]
    llm_used: bool
    provenance: dict[str, Any]
    validation: dict[str, Any]
    citation_check: str
    rejected_llm_validation: dict[str, Any] | None
    fallback_reason: str | None
    failure_diagnostic: dict[str, Any] | None
    retrieval_metrics: dict[str, Any]
    context: dict[str, Any]


# --- agent runs -----------------------------------------------------------------------

class AgentStep(ApiModel):
    id: int
    run_id: int
    node_name: str
    agent_role: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any]
    latency_ms: int
    started_at: str
    finished_at: str


class AgentRunTrace(ApiModel):
    id: int
    case_id: int
    question: str
    route: str
    status: str
    retrieval_mode: str
    final_answer: str
    total_ms: int
    runtime: str
    checkpoint_thread_id: str
    resume_count: int
    created_at: str
    finished_at: str
    conversation_id: int | None
    resumable: bool
    steps: list[AgentStep]
    # Read back from runs saved by earlier releases; shape not migrated.
    citations: list[dict[str, Any]]
    usage: dict[str, Any]
