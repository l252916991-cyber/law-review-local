"""Typed host read port (E04): the seven frozen HostReadPort methods.

Contract: app/runtime_ports.py::HostReadPort (protocol v2). Every call:

1. re-checks the current principal, ``view`` permission and case scope;
2. verifies the pinned source identity when one is declared (fail closed when
   the authorized case or statute corpus changed);
3. enforces the frozen query/limit/page bounds instead of truncating;
4. returns typed records and records the access without case text.

Unsupported capabilities are rejected explicitly (``UnsupportedCapabilityError``
or ``ContractValidationError``): an unconfigured statute corpus, remote
embedding backends in an offline deployment, a call bound to another scope, or
out-of-range limits. Empty results are only returned for genuinely empty data.

The port reads through ``app.db.connect()``, so it follows the active
``db_scope``: the M2 pilot points it at its private snapshot, production entry
points at the business database. It never writes business tables; M4 adds the
accepted business audit, M2 records into the isolated experiment log.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .agent_tools import _search
from .db import SCHEMA_VERSION, connect, get_db_path
from .legal_corpus import LegalCorpus
from .rag import recall_memories
from .runtime_ports import (
    CATALOG_LIMIT_DEFAULT,
    CATALOG_LIMIT_MAX,
    CATALOG_LIMIT_MIN,
    DEFAULT_TENANT_ID,
    MAX_PAGE_TEXT_CHARS,
    MAX_QUERY_CHARS,
    SEARCH_LIMIT_DEFAULT,
    SEARCH_LIMIT_MAX,
    SEARCH_LIMIT_MIN,
    AccessDeniedError,
    ContractValidationError,
    DocumentRef,
    EvidenceRef,
    GapRecord,
    MemoryRecord,
    PageContent,
    RunScope,
    SearchHit,
    SearchResult,
    StatuteHit,
    UnsupportedCapabilityError,
)
from .security import require_case_access, require_permission
from .statutory_retrieval import retrieve_statutory

#: Records one authorized read: (kind, detail without case text).
AccessRecorder = Callable[[str, dict[str, Any]], None]

GAP_SEVERITIES = frozenset({"高", "中", "低"})
MEMORY_LIMIT_MAX = 50
STATUTE_LIMIT_MAX = 20

#: Typed fields of SearchHit; everything else stays in ``extra`` verbatim.
_SEARCH_TYPED_FIELDS = frozenset({
    "page_id", "page_no", "document_id", "name", "doc_type", "quote", "text",
    "rank", "retrieval_explain", "channels", "is_neighbor",
})
_MEMORY_TYPED_FIELDS = frozenset({
    "id", "content", "importance", "source_run_id", "kind", "created_at",
})
_STATUTE_TYPED_FIELDS = frozenset({"law_name", "article_number", "text", "retrieval_score"})

CASE_TABLES: dict[str, str] = {
    "cases": "SELECT * FROM cases WHERE id=? ORDER BY id",
    "documents": "SELECT * FROM documents WHERE case_id=? ORDER BY id",
    "pages": "SELECT p.* FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.case_id=? ORDER BY p.id",
    "evidence": "SELECT * FROM evidence WHERE case_id=? ORDER BY id",
    "agent_runs": "SELECT * FROM agent_runs WHERE case_id=? ORDER BY id",
    "memories": "SELECT * FROM memories WHERE case_id=? ORDER BY id",
    "gap_detections": "SELECT * FROM gap_detections WHERE case_id=? ORDER BY id",
}


class SnapshotChangedError(RuntimeError):
    """The authorized source changed; the pinned snapshot must be rebuilt."""


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def capture_case_rows(
    case_id: int,
    *,
    source_path: Path | None = None,
    max_pages: int | None = None,
    max_chars: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read one authorized case from a SQLite source in a single read-only transaction.

    ``source_path`` defaults to the active database scope. The source schema must
    already be migrated by its owning application; bounds are enforced instead of
    truncating, and a missing case is an authorization failure.
    """
    path = Path(source_path) if source_path is not None else get_db_path()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        if source.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("source schema must be migrated by its owning application first")
        # Reject before materializing page bodies, not after allocating an
        # arbitrarily large case in Python. The aggregate shares this snapshot.
        size = source.execute(
            "SELECT COUNT(*),COALESCE(SUM(LENGTH(p.text)),0),COALESCE(MAX(LENGTH(p.text)),0) "
            "FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.case_id=?", (case_id,),
        ).fetchone()
        if ((max_pages is not None and size[0] > max_pages)
                or (max_chars is not None and size[1] > max_chars)):
            raise ValueError("case exceeds the declared snapshot bounds; no truncation is performed")
        if size[2] > MAX_PAGE_TEXT_CHARS:
            raise ValueError(f"page exceeds the framework's {MAX_PAGE_TEXT_CHARS}-character limit")
        rows = {
            table: [dict(row) for row in source.execute(sql, (case_id,))]
            for table, sql in CASE_TABLES.items()
        }
    if not rows["cases"]:
        raise AccessDeniedError("case is unavailable")
    pages = rows["pages"]
    if max_pages is not None and len(pages) > max_pages:
        raise ValueError("case exceeds the declared snapshot bounds; no truncation is performed")
    if max_chars is not None and sum(len(row["text"]) for row in pages) > max_chars:
        raise ValueError("case exceeds the declared snapshot bounds; no truncation is performed")
    if any(len(row["text"]) > MAX_PAGE_TEXT_CHARS for row in pages):
        raise ValueError(f"page exceeds the framework's {MAX_PAGE_TEXT_CHARS}-character limit")
    return rows


def case_identity(rows: dict[str, list[dict[str, Any]]]) -> str:
    """Content identity of one authorized case snapshot."""
    return canonical_digest(rows)


def _bounded_int(value: Any, *, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractValidationError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _bounded_query(query: Any) -> str:
    if not isinstance(query, str):
        raise ContractValidationError("query must be a string")
    stripped = query.strip()
    if not 1 <= len(stripped) <= MAX_QUERY_CHARS:
        raise ContractValidationError(f"query length must be 1..{MAX_QUERY_CHARS} characters")
    return stripped


def search_hit_from_legacy(item: dict[str, Any]) -> SearchHit:
    """Map one controlled-search result onto the typed contract record."""
    extra = {key: value for key, value in item.items() if key not in _SEARCH_TYPED_FIELDS}
    return SearchHit(
        page_id=int(item.get("page_id") or 0),
        page_no=int(item.get("page_no") or 0),
        document_id=int(item.get("document_id") or 0),
        name=str(item.get("name") or ""),
        doc_type=str(item.get("doc_type") or ""),
        quote=str(item.get("quote") or ""),
        text=str(item.get("text") or ""),
        rank=int(item.get("rank") or 0),
        retrieval_explain=str(item.get("retrieval_explain") or ""),
        channels=tuple(item.get("channels") or ()),
        is_neighbor=bool(item.get("is_neighbor") or False),
        extra=extra,
    )


def search_hit_context(hit: SearchHit) -> dict[str, Any]:
    """Rebuild the domain context dict for the original expert functions."""
    return {
        **hit.extra,
        "page_id": hit.page_id,
        "page_no": hit.page_no,
        "document_id": hit.document_id,
        "name": hit.name,
        "doc_type": hit.doc_type,
        "quote": hit.quote,
        "text": hit.text,
        "rank": hit.rank,
        "retrieval_explain": hit.retrieval_explain,
        "channels": list(hit.channels),
        "is_neighbor": hit.is_neighbor,
    }


def memory_record_from_legacy(row: dict[str, Any]) -> MemoryRecord:
    extra = {key: value for key, value in row.items() if key not in _MEMORY_TYPED_FIELDS}
    return MemoryRecord(
        memory_id=int(row.get("id") or 0),
        content=str(row.get("content") or ""),
        importance=float(row.get("importance") or 0.0),
        source_run_id=row.get("source_run_id"),
        kind=str(row.get("kind") or ""),
        created_at=str(row.get("created_at") or ""),
        extra=extra,
    )


def memory_record_context(record: MemoryRecord) -> dict[str, Any]:
    return {
        **record.extra,
        "id": record.memory_id,
        "content": record.content,
        "importance": record.importance,
        "source_run_id": record.source_run_id,
        "kind": record.kind,
        "created_at": record.created_at,
    }


def statute_hit_from_legacy(hit: dict[str, Any], corpus_identity: str) -> StatuteHit:
    extra = {key: value for key, value in hit.items() if key not in _STATUTE_TYPED_FIELDS}
    return StatuteHit(
        law_name=str(hit.get("law_name") or ""),
        article_no=str(hit.get("article_number") or ""),
        text=str(hit.get("text") or ""),
        score=float(hit.get("retrieval_score") or 0.0),
        corpus_version=corpus_identity,
        extra=extra,
    )


class LocalHostReadPort:
    """Authorized, typed read surface bound to one RunScope.

    The instance is bound to the scope it was created for: a call carrying a
    different tenant or case is rejected instead of silently reading another
    case. ``expected_source_identity`` enables the pinned-version check.
    """

    def __init__(
        self,
        scope: RunScope,
        *,
        access_recorder: AccessRecorder | None = None,
        expected_source_identity: str | None = None,
        identity_source_path: Path | None = None,
        allow_remote_embeddings: bool = False,
        corpus: LegalCorpus | None = None,
        corpus_identity: str = "unconfigured",
        max_pages: int | None = None,
        max_chars: int | None = None,
    ) -> None:
        if scope.tenant_id != DEFAULT_TENANT_ID:
            raise ContractValidationError(
                f"tenant_id {scope.tenant_id!r} is not supported by this single-tenant host"
            )
        if not isinstance(scope.case_id, int) or scope.case_id < 1:
            raise ContractValidationError("scope.case_id must be a positive integer")
        self.scope = scope
        self.access_recorder = access_recorder
        self.expected_source_identity = expected_source_identity
        # The pinned identity always describes the authorized SOURCE, while the
        # reads follow the active db scope (the M2 session reads its private
        # snapshot copy). Without this split, a snapshot that deliberately
        # clears source file paths could never match the source identity.
        self.identity_source_path = identity_source_path
        self.allow_remote_embeddings = allow_remote_embeddings
        self.corpus = corpus
        self.corpus_identity = corpus_identity
        self.max_pages = max_pages
        self.max_chars = max_chars

    # -- shared guards ------------------------------------------------------

    def _bind(self, scope: RunScope) -> int:
        if scope != self.scope:
            raise AccessDeniedError("port call scope does not match the bound scope")
        try:
            require_permission("view")
            require_case_access(scope.case_id)
        except HTTPException as exc:
            raise AccessDeniedError(f"case {scope.case_id}: {exc.detail}") from exc
        return scope.case_id

    def _record(self, kind: str, **detail: Any) -> None:
        if self.access_recorder is not None:
            self.access_recorder(kind, {"case_id": self.scope.case_id, **detail})

    def _remote_guard(self, use_remote_embeddings: bool) -> None:
        if use_remote_embeddings and not self.allow_remote_embeddings:
            raise UnsupportedCapabilityError(
                "remote embedding/rerank backends are disabled in this deployment; "
                "request offline (hashed-local) retrieval explicitly"
            )

    def assert_current(self) -> None:
        """Pinned-version check: the authorized case data must be unchanged."""
        if self.expected_source_identity is None:
            return
        if self.source_identity() != self.expected_source_identity:
            raise SnapshotChangedError("authorized case data changed; start a new session/snapshot")

    def source_identity(self) -> str:
        try:
            rows = capture_case_rows(
                self.scope.case_id,
                source_path=self.identity_source_path,
                max_pages=self.max_pages,
                max_chars=self.max_chars,
            )
        except ValueError as exc:
            # Schema/bounds failures are contract violations, not read results.
            raise ContractValidationError(str(exc)) from exc
        return case_identity(rows)

    # -- HostReadPort -------------------------------------------------------

    def list_documents(
        self, scope: RunScope, *, limit: int = CATALOG_LIMIT_DEFAULT
    ) -> tuple[DocumentRef, ...]:
        case_id = self._bind(scope)
        limit = _bounded_int(
            limit, name="limit", default=CATALOG_LIMIT_DEFAULT,
            minimum=CATALOG_LIMIT_MIN, maximum=CATALOG_LIMIT_MAX,
        )
        self.assert_current()
        with closing(connect()) as conn:
            rows = conn.execute(
                """SELECT id,name,mime_type,pages,status,doc_type,people,date_range,summary,
                          created_at,updated_at
                   FROM documents WHERE case_id=? ORDER BY id LIMIT ?""",
                (case_id, limit),
            ).fetchall()
        records = tuple(
            DocumentRef(
                document_id=int(row["id"]),
                name=str(row["name"] or ""),
                doc_type=str(row["doc_type"] or ""),
                mime_type=str(row["mime_type"] or ""),
                page_count=int(row["pages"] or 0),
                status=str(row["status"] or ""),
                people=str(row["people"] or ""),
                date_range=str(row["date_range"] or ""),
                summary=str(row["summary"] or ""),
                created_at=str(row["created_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
            )
            for row in rows
        )
        self._record("list_documents", count=len(records))
        return records

    def list_evidence(
        self, scope: RunScope, *, limit: int = CATALOG_LIMIT_DEFAULT
    ) -> tuple[EvidenceRef, ...]:
        case_id = self._bind(scope)
        limit = _bounded_int(
            limit, name="limit", default=CATALOG_LIMIT_DEFAULT,
            minimum=CATALOG_LIMIT_MIN, maximum=CATALOG_LIMIT_MAX,
        )
        self.assert_current()
        with closing(connect()) as conn:
            rows = conn.execute(
                """SELECT e.id,e.title,e.category,e.fact,e.credibility,e.status,
                          e.source_document_id,e.source_page_start,e.source_page_end,
                          e.quote,e.created_at,d.name AS source_name
                   FROM evidence e LEFT JOIN documents d ON d.id=e.source_document_id
                   WHERE e.case_id=? ORDER BY e.id LIMIT ?""",
                (case_id, limit),
            ).fetchall()
        records = tuple(
            EvidenceRef(
                evidence_id=int(row["id"]),
                title=str(row["title"] or ""),
                category=str(row["category"] or ""),
                fact=str(row["fact"] or ""),
                credibility=str(row["credibility"] or ""),
                status=str(row["status"] or ""),
                source_document_id=row["source_document_id"],
                source_page_start=row["source_page_start"],
                source_page_end=row["source_page_end"],
                quote=str(row["quote"] or ""),
                source_name=str(row["source_name"] or ""),
                created_at=str(row["created_at"] or ""),
            )
            for row in rows
        )
        self._record("list_evidence", count=len(records))
        return records

    def search(
        self,
        scope: RunScope,
        query: str,
        *,
        limit: int = SEARCH_LIMIT_DEFAULT,
        use_remote_embeddings: bool = True,
    ) -> SearchResult:
        case_id = self._bind(scope)
        query = _bounded_query(query)
        limit = _bounded_int(
            limit, name="limit", default=SEARCH_LIMIT_DEFAULT,
            minimum=SEARCH_LIMIT_MIN, maximum=SEARCH_LIMIT_MAX,
        )
        self._remote_guard(use_remote_embeddings)
        self.assert_current()
        items, metrics = _search(case_id, {"query": query, "limit": limit}, use_remote_embeddings)
        hits = tuple(search_hit_from_legacy(item) for item in items)
        self._record(
            "search", count=len(hits),
            backend=str(metrics.get("query_embedding_backend") or metrics.get("mode") or ""),
        )
        return SearchResult(hits=hits, metrics=metrics)

    def read_page(self, scope: RunScope, document_id: int, page_no: int) -> PageContent:
        case_id = self._bind(scope)
        if type(document_id) is not int or document_id < 1:
            raise ContractValidationError("document_id must be a positive integer")
        if type(page_no) is not int or page_no < 1:
            raise ContractValidationError("page_no must be a positive integer")
        self.assert_current()
        with closing(connect()) as conn:
            row = conn.execute(
                """SELECT p.text,p.summary FROM pages p JOIN documents d ON d.id=p.document_id
                   WHERE d.case_id=? AND p.document_id=? AND p.page_no=?""",
                (case_id, document_id, page_no),
            ).fetchone()
        if row is None:
            raise AccessDeniedError("page is outside this authorized case")
        text = str(row["text"] or "")
        if len(text) > MAX_PAGE_TEXT_CHARS:
            raise ContractValidationError(
                f"page exceeds the {MAX_PAGE_TEXT_CHARS}-character port limit; no truncation is performed"
            )
        self._record("read_page", document_id=document_id, page_no=page_no)
        return PageContent(
            document_id=document_id,
            page_no=page_no,
            text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            summary=str(row["summary"] or ""),
        )

    def recall_memories(
        self,
        scope: RunScope,
        query: str,
        *,
        limit: int = 3,
        use_remote_embeddings: bool = True,
    ) -> tuple[MemoryRecord, ...]:
        case_id = self._bind(scope)
        query = _bounded_query(query)
        limit = _bounded_int(limit, name="limit", default=3, minimum=1, maximum=MEMORY_LIMIT_MAX)
        self._remote_guard(use_remote_embeddings)
        self.assert_current()
        rows = recall_memories(case_id, query, limit, use_remote_embeddings)
        records = tuple(memory_record_from_legacy(row) for row in rows)
        self._record("recall_memories", count=len(records))
        return records

    def search_statutes(
        self, scope: RunScope, query: str, *, limit: int = SEARCH_LIMIT_DEFAULT
    ) -> tuple[StatuteHit, ...]:
        self._bind(scope)
        query = _bounded_query(query)
        limit = _bounded_int(
            limit, name="limit", default=SEARCH_LIMIT_DEFAULT,
            minimum=SEARCH_LIMIT_MIN, maximum=STATUTE_LIMIT_MAX,
        )
        if self.corpus is None:
            raise UnsupportedCapabilityError(
                "public statute corpus is not configured for this host; statute search is unavailable"
            )
        self.assert_current()
        hits = self.corpus.search(query, limit=limit)
        records = tuple(statute_hit_from_legacy(hit, self.corpus_identity) for hit in hits)
        self._record("search_statutes", count=len(records))
        return records

    def statutory_lookup(self, scope: RunScope, query: str, *, limit: int = 5) -> dict[str, Any]:
        """Domain-shaped statute lookup for the pinned corpus (authorized + version-checked).

        Keeps the existing ``retrieve_statutory`` contract (status/mode/summary/
        hits/warning) for the statutory expert; ``search_statutes`` above is the
        typed public surface.
        """
        self._bind(scope)
        query = _bounded_query(query)
        self.assert_current()
        if self.corpus is None:
            return retrieve_statutory(query, limit=limit, corpus=None)
        result = retrieve_statutory(query, limit=limit, corpus=self.corpus)
        self._record("statutory_lookup", status=str(result.get("status") or ""))
        return result

    def saved_gap_analysis(
        self,
        scope: RunScope,
        *,
        severity: str | None = None,
        limit: int = CATALOG_LIMIT_DEFAULT,
    ) -> tuple[GapRecord, ...]:
        case_id = self._bind(scope)
        if severity is not None and severity not in GAP_SEVERITIES:
            raise ContractValidationError("severity must be one of 高, 中, 低")
        limit = _bounded_int(
            limit, name="limit", default=CATALOG_LIMIT_DEFAULT,
            minimum=CATALOG_LIMIT_MIN, maximum=CATALOG_LIMIT_MAX,
        )
        self.assert_current()
        sql = "SELECT * FROM gap_detections WHERE case_id=?"
        params: list[Any] = [case_id]
        if severity:
            sql += " AND severity=?"
            params.append(severity)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with closing(connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        records = []
        for row in rows:
            try:
                affected = json.loads(row["affected_evidence_ids"] or "[]")
            except (TypeError, json.JSONDecodeError):
                affected = []
            records.append(
                GapRecord(
                    gap_id=int(row["id"]),
                    run_id=row["run_id"],
                    gap_type=str(row["gap_type"] or ""),
                    severity=str(row["severity"] or ""),
                    description=str(row["description"] or ""),
                    suggestion=str(row["suggestion"] or ""),
                    affected_evidence_ids=tuple(int(item) for item in affected),
                    created_at=str(row["created_at"] or ""),
                )
            )
        self._record("saved_gap_analysis", count=len(records), severity=severity or "")
        return tuple(records)
