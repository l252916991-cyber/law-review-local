"""M2 host boundary: original domain nodes over an authorized case snapshot.

The source connection is SQLite read-only. Caches and legacy comparison writes
belong to a private temporary database containing this case only. Nothing here
imports ForgeHarness: the governed scheduler lives in a separate interpreter.
This engineering pilot deliberately supports offline inference only.

E04 upgrade: every host read now goes through the typed, authorized and
version-checked ``LocalHostReadPort`` (app/host_ports.py) instead of ad-hoc
internal calls, while the session keeps bracketing each node with its own
pinned-source check (pre ``assert_current`` + post check on mutation during
computation).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import agents
from .db import connect, db_scope, get_db_path, init_db
from .forge_sources import statute_source, verify_statutes
from .forge_governance import BackendLedger
from .host_ports import (
    LocalHostReadPort,
    SnapshotChangedError,
    canonical_digest,
    capture_case_rows,
    case_identity,
    memory_record_context,
    search_hit_context,
)
from .legal_corpus import LegalCorpus
from .runtime_adapter import current_actor_identity
from .runtime_ports import AccessDeniedError, DEFAULT_TENANT_ID, RunRequest
from .security import require_case_access, require_permission


class CitationIntegrityError(ValueError):
    """A retrieved display quote cannot be mapped onto the exact source page."""


class CitationIntegrityError(ValueError):
    """A retrieved display quote cannot be mapped onto the exact source page."""


def exact_quote(text: str, display: str) -> str:
    """Recover a raw span from the existing whitespace-normalized UI excerpt.

    Truncation ellipses are display decoration. The returned span preserves
    raw whitespace; an unrelated or stale quote is rejected, never substituted.
    """
    core = display.strip().strip("…").strip()
    if not core:
        raise CitationIntegrityError("empty source quote")
    pattern = r"\s+".join(re.escape(part) for part in core.split())
    match = re.search(pattern, text)
    if match is None:
        raise CitationIntegrityError("retrieved quote is not a span of its source page")
    span = match.group()
    if "」" in span or "[page:" in span:
        raise CitationIntegrityError("quote cannot be represented by the strict citation grammar")
    return span


def capture_corpus() -> tuple[str, LegalCorpus | None]:
    """Validate the exact current public corpus without creating a case session."""
    configured = os.getenv("LAW_REVIEW_LEGAL_CORPUS_DIR", "").strip()
    if not configured:
        return "unconfigured", None
    directory = Path(configured).resolve(strict=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.stat().st_size > 1_000_000:
        raise ValueError("public statute manifest exceeds the pilot bound")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if len(manifest["documents"]) > 1000:
        raise ValueError("too many public statute documents")
    paths = [directory / item["document_file"] for item in manifest["documents"]]
    if any(path.resolve().parent != directory for path in paths):
        raise ValueError("statute document is outside its corpus")
    if sum(path.stat().st_size for path in paths) > 20_000_000:
        raise ValueError("public statute corpus exceeds the pilot bound")
    corpus = LegalCorpus(directory)
    if manifest_path.read_bytes() != raw:
        raise SnapshotChangedError("public statute manifest changed while reading")
    return hashlib.sha256(raw).hexdigest(), corpus


class ReadonlyCaseSession:
    """One trusted request, one case snapshot, fixed allowlisted domain nodes.

    A session is ephemeral, non-resumable and never a public API runtime. Each
    node revalidates the current principal, permission and source identity.
    The framework receives node results, not SQL, database paths or credentials.
    """

    def __init__(self, request: RunRequest, *, max_pages: int = 500,
                 max_chars: int = 2_000_000) -> None:
        request.validate()
        if request.runtime != "forgeharness-readonly":
            raise ValueError("M2 requires the explicit forgeharness-readonly runtime")
        from .forge_bridge import framework_binding
        if request.framework_version not in ("", framework_binding()[0]["version_label"]):
            raise ValueError("unsupported requested framework version")
        if len(request.question.strip()) > 500:
            raise ValueError("M2 question exceeds the host search port's 500-character bound")
        if request.scope.tenant_id != DEFAULT_TENANT_ID:
            raise ValueError("unsupported tenant")
        if request.model.use_llm or request.use_remote_embeddings:
            raise ValueError("M2 engineering pilot requires offline model and embeddings")
        if request.persist_memory:
            raise ValueError("M2 requests must explicitly disable memory persistence")
        if request.execution_mode != "sync" or request.memory_snapshot is not None:
            raise ValueError("M2 accepts synchronous requests and host-owned memory snapshots only")
        if request.model.model is not None or request.model.timeout_seconds is not None or request.budget is not None:
            raise ValueError("M2 does not accept unimplemented model or budget overrides")
        if not 1 <= max_pages <= 2000 or not 1 <= max_chars <= 20_000_000:
            raise ValueError("invalid snapshot bounds")
        self.request = deepcopy(request)
        self.actor = current_actor_identity()
        if self.actor != request.actor:
            raise AccessDeniedError("request actor does not match the authenticated principal")
        self.source = get_db_path().resolve(strict=True)
        self.max_pages, self.max_chars = max_pages, max_chars
        self.outputs: dict[str, Any] = {}
        self.access_log: list[dict[str, str]] = []
        self.port_access_log: list[dict[str, Any]] = []
        self.backends = BackendLedger(self.assert_current)
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._authorize()
        rows = self._capture()
        self._case_identity = case_identity(rows)
        self.corpus_identity, self._corpus = self._capture_corpus()
        self.source_identity = canonical_digest({"case": self._case_identity, "corpus": self.corpus_identity})
        if request.data_snapshot_version and request.data_snapshot_version != self.source_identity:
            raise SnapshotChangedError("requested source snapshot is not current")
        self._temporary = tempfile.TemporaryDirectory(prefix="lexvault-m2-private-")
        self.directory = Path(self._temporary.name)
        self.snapshot = self.directory / "host" / "law_review.db"
        try:
            with self._scope(self.snapshot):
                init_db(seed=False)
                with closing(connect()) as target, target:
                    for table, records in rows.items():
                        for original in records:
                            row = dict(original)
                            if table == "documents":
                                row["stored_path"] = ""  # page text only; no source-file access
                            columns = ",".join('"' + name.replace('"', '""') + '"' for name in row)
                            markers = ",".join("?" for _ in row)
                            target.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({markers})', tuple(row.values()))
            self.port = LocalHostReadPort(
                request.scope,
                access_recorder=self._record_port_access,
                expected_source_identity=self._case_identity,
                identity_source_path=self.source,
                allow_remote_embeddings=False,
                corpus=self._corpus,
                corpus_identity=self.corpus_identity,
                max_pages=self.max_pages,
                max_chars=self.max_chars,
            )
            self.coordinator = agents.LawReviewCoordinator(request.scope.case_id, False)
            self.coordinator.statutory_conflict_agent = self._statutory_agent()
            self.specs = agents.specialist_specs(self.coordinator)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> ReadonlyCaseSession:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    @contextmanager
    def _scope(self, path: Path):
        with db_scope(path):
            if get_db_path().resolve() != path.resolve():
                raise RuntimeError("database override prevents safe snapshot isolation")
            with self.backends.activate():
                yield

    def _authorize(self) -> None:
        if self._temporary is None and hasattr(self, "snapshot"):
            raise RuntimeError("session is closed")
        if current_actor_identity() != self.actor:
            raise AccessDeniedError("execution identity changed")
        try:
            require_permission("view")
            require_case_access(self.request.scope.case_id)
        except HTTPException as exc:
            raise AccessDeniedError("current authorization does not cover this operation") from exc

    def _record_port_access(self, kind: str, detail: dict[str, Any]) -> None:
        """Isolated experiment log: authorized reads, never case text."""
        self.port_access_log.append({"access": kind, **detail})

    def _capture(self) -> dict[str, list[dict[str, Any]]]:
        # URI mode=ro prevents accidental creation, migrations and business writes.
        return capture_case_rows(
            self.request.scope.case_id,
            source_path=self.source,
            max_pages=self.max_pages,
            max_chars=self.max_chars,
        )

    def assert_current(self) -> None:
        self._authorize()
        if case_identity(self._capture()) != self._case_identity:
            raise SnapshotChangedError("source case changed or was withdrawn")
        try:
            corpus_identity = self._capture_corpus()[0]
        except (OSError, ValueError, KeyError) as exc:
            raise SnapshotChangedError("public statute corpus is unavailable or changed") from exc
        if corpus_identity != self.corpus_identity:
            raise SnapshotChangedError("public statute corpus changed")

    def _capture_corpus(self) -> tuple[str, LegalCorpus | None]:
        return capture_corpus()

    def _statutory_agent(self) -> agents.StatutoryConflictAgent:
        if self._corpus is None:
            return agents.StatutoryConflictAgent()
        # Authorized + version-checked lookup through the typed host port.
        return agents.StatutoryConflictAgent(
            lambda question: self.port.statutory_lookup(self.request.scope, question, limit=5)
        )

    def read_page(self, document_id: int, page_no: int) -> dict[str, Any]:
        self.assert_current()
        with self._scope(self.snapshot):
            page = self.port.read_page(self.request.scope, document_id, page_no)
        return {"document_id": page.document_id, "page_no": page.page_no,
                "text": page.text, "text_sha256": page.text_sha256}

    def call(self, node: str) -> dict[str, Any]:
        """Fixed node dispatch: arguments and dependencies remain host-owned."""
        self.assert_current()
        if node in self.outputs:
            raise ValueError("a node cannot be executed twice in this one-shot pilot")
        with self._scope(self.snapshot):
            output = self._call(node)
        self.assert_current()  # revocation or mutation during computation fails closed
        self.outputs[node] = deepcopy(output)
        self.access_log.append({"node": node, "source_identity": self.source_identity})
        return deepcopy(output)

    def _call(self, node: str) -> dict[str, Any]:
        question = self.request.question.strip()
        if node == "planner":
            route, plan = self.coordinator.planner_agent.build_plan(question)
            return {"route": route, "nodes": [asdict(item) for item in plan]}
        if "planner" not in self.outputs:
            raise ValueError("planner must complete first")
        if node == "memory_recall":
            records = self.port.recall_memories(
                self.request.scope, question, limit=3, use_remote_embeddings=False
            )
            return {"items": [memory_record_context(record) for record in records]}
        if node == "retrieve":
            if "memory_recall" not in self.outputs:
                raise ValueError("memory snapshot must be frozen before retrieval")
            result = self.port.search(
                self.request.scope, question, limit=6, use_remote_embeddings=False
            )
            return {"contexts": [search_hit_context(hit) for hit in result.hits],
                    "metrics": result.metrics}
        if "retrieve" not in self.outputs:
            raise ValueError("retrieval must complete first")
        contexts = self.outputs["retrieve"]["contexts"]
        plan = {item["name"]: item for item in self.outputs["planner"]["nodes"]}
        if node in self.specs and node in plan:
            if not set(plan[node]["depends_on"]) <= self.outputs.keys():
                raise ValueError("specialist dependencies have not completed")
            spec = self.specs[node]
            return agents.validate_specialist_output(spec.runner(question, deepcopy(contexts)),
                                                     contexts, schema=spec.output_schema)
        if node == "critic":
            if not set(plan["critic"]["depends_on"]) <= self.outputs.keys():
                raise ValueError("critic dependencies have not completed")
            output = self.coordinator.critic_agent.run(
                question, self.outputs["planner"]["route"], deepcopy(contexts),
                {name: deepcopy(self.outputs[name]) for name in self.specs if name in plan}, False,
            )
            output["summary"] = f"引用检查 {output['citation_check']}，LLM=off"
            return output
        if node == "finish" and "critic" in self.outputs:
            return self._finish(contexts)
        raise ValueError("unsupported node or unmet dependencies")

    def _finish(self, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        bundle_pages = []
        strict_quotes = {}
        for index, item in enumerate(contexts, 1):
            page = self.port.read_page(self.request.scope, item["document_id"], item["page_no"])
            strict_quotes[index] = exact_quote(page.text, item["quote"])
            bundle_pages.append({"page_no": index, "document_id": item["document_id"],
                                 "document_name": item["name"], "document_page_no": item["page_no"],
                                 "text": page.text, "text_sha256": page.text_sha256})
        if len(bundle_pages) > 100 or sum(len(page["text"]) for page in bundle_pages) > 200_000:
            raise ValueError("retrieved pages exceed the framework bundle limit")
        answer = self.outputs["critic"]["answer"]

        def replace_marker(match: re.Match[str]) -> str:
            index = int(match.group(1))
            if index not in strict_quotes:
                raise CitationIntegrityError("answer cites an unavailable source")
            return f"[page:{index}]「{strict_quotes[index]}」"

        strict_answer = re.sub(r"\[\s*资料\s*(\d+)\s*\]", replace_marker, answer)
        statutes = []
        law_lines = []
        if "statutory_conflict" in self.outputs and self._corpus is not None:
            lookup = self.port.statutory_lookup(self.request.scope, self.request.question.strip())
            # Lexical discovery candidates do not become verified citations.
            if lookup["status"] == "ok":
                for index, hit in enumerate(lookup["hits"], 1):
                    source = statute_source(hit, self.corpus_identity, index)
                    quote = source["text"][:300].split("」", 1)[0]
                    if not quote.strip():
                        raise CitationIntegrityError("statute cannot be represented as an exact quote")
                    statutes.append(source)
                    law_lines.append(f"{source['law_name']} 第{source['article_number']}条 "
                                     f"({source['version_date']}) [statute:{index}]「{quote}」")
        validation = self.outputs["critic"]["validation"]
        projection = "legacy-case-answer"
        if law_lines:
            appendix = "\n\n已核验法条原文：\n" + "\n".join(law_lines)
            if bundle_pages:
                strict_answer += appendix
            else:
                # A public statute is a distinct source kind, not a case page.
                answer = "仅列示已核验法条原文，不作案件事实判断。" + appendix + "\n请律师复核适用版本与法律适用。"
                strict_answer = answer
                projection = "statute-source-v1"
                passed, reason = verify_statutes(strict_answer, statutes,
                                                corpus_version=self.corpus_identity)
                validation = {"valid": passed, "scope": "statutory_source_integrity",
                              "semantic_entailment_checked": False,
                              "amount_calculations_checked": False, "reason": reason}
        return {"answer": answer, "strict_answer": strict_answer,
                "citations": [agents._citation(item, i) for i, item in enumerate(contexts, 1)],
                "bundle": {"schema_version": 1, "tenant_id": DEFAULT_TENANT_ID,
                           "case_id": str(self.request.scope.case_id), "title": "Authorized M2 snapshot",
                           "version": self.source_identity, "synthetic": False, "pages": bundle_pages},
                "validation": validation, "statutory_sources": statutes,
                "source_contract_version": 1, "answer_projection": projection,
                "memory_persisted": False, "source_identity": self.source_identity}

    def compare_native(self, *, shared_inputs: bool = True) -> dict[str, Any]:
        """Compare either shared inputs or independent offline retrieval paths."""
        self.assert_current()
        if "finish" not in self.outputs:
            raise ValueError("framework domain run must finish before comparison")
        native_db = self.directory / ("native" if shared_inputs else "native-independent") / "law_review.db"
        native_db.parent.mkdir()
        with closing(sqlite3.connect(self.snapshot)) as source, closing(sqlite3.connect(native_db)) as target:
            source.backup(target)
        retrieval = deepcopy(self.outputs["retrieve"])

        class FrozenRetrieval:
            def retrieve(self, query: str, limit: int = 6) -> dict[str, Any]:
                return deepcopy(retrieval)

        with self._scope(native_db):
            coordinator = agents.LawReviewCoordinator(self.request.scope.case_id, False)
            coordinator.statutory_conflict_agent = self._statutory_agent()
            if shared_inputs:
                coordinator.retrieval_agent = FrozenRetrieval()
            result = coordinator.process_query(
                self.request.question.strip(), use_llm=False, persist_memory=False,
                memory_snapshot=deepcopy(self.outputs["memory_recall"]["items"]) if shared_inputs else None,
            )
            trace = agents.get_run_trace(result["run_id"])
        specialists = {step["node_name"]: step["output"] for step in trace["steps"]
                       if step["node_name"] in self.specs}
        checks = {"answer": result["answer"] == self.outputs["critic"]["answer"],
                  "citations": result["citations"] == self.outputs["finish"]["citations"],
                  "validation": result["validation"] == self.outputs["critic"]["validation"],
                  "specialists": specialists == {key: self.outputs[key] for key in self.specs
                                                   if key in self.outputs}}
        self.assert_current()
        return {"mode": "shared_retrieval_and_memory_snapshot" if shared_inputs else "independent_retrieval_and_memory",
                "checks": checks,
                "answer_comparison_scope": "legacy_critic_before_source_projection",
                "answer_projection": self.outputs["finish"]["answer_projection"],
                "passed": all(checks.values()), "source_identity": self.source_identity,
                "model_quality_assessed": False, "source_business_writes": 0}
