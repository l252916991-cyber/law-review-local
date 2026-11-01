"""E04 acceptance tests: the typed host read port.

Plan exit criteria: catalog/document/evidence, retrieval, page reading,
memory, statutes and saved-gap reads each have a normal path plus
unauthorized / revoked / changed-source cases, and unsupported capabilities
are rejected explicitly (never an empty result or a silent truncation).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

TEST_DATA = tempfile.mkdtemp(prefix="lexvault-host-ports-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA

from app.agent_tools import _search as legacy_search  # noqa: E402
from app.db import connect, init_db, now, transaction  # noqa: E402
from app.host_ports import (  # noqa: E402
    LocalHostReadPort,
    SnapshotChangedError,
    capture_case_rows,
    case_identity,
    memory_record_context,
    search_hit_context,
)
from app.legal_corpus import SCHEMA_VERSION as CORPUS_SCHEMA  # noqa: E402
from app.legal_corpus import LegalCorpus, split_articles  # noqa: E402
from app.rag import hashed_embedding  # noqa: E402
from app.runtime_ports import (  # noqa: E402  # noqa: E402
    CATALOG_LIMIT_MAX,
    MAX_QUERY_CHARS,
    SEARCH_LIMIT_MAX,
    AccessDeniedError,
    ContractValidationError,
    DocumentRef,
    EvidenceRef,
    GapRecord,
    MemoryRecord,
    RunScope,
    UnsupportedCapabilityError,
)
from app.security import Principal, _principal  # noqa: E402
from tests.support import IsolatedDatabaseTestCase  # noqa: E402

QUERY = "固定回报审批"


@contextmanager
def scoped_principal(case_ids, name="受限律师"):
    marker = _principal.set(
        Principal(name, False, tuple(case_ids), permissions=frozenset({"view"}))
    )
    try:
        yield
    finally:
        _principal.reset(marker)


def write_corpus(directory: Path, text: str = "当前受控合成原文。"):
    document = {
        "schema_version": CORPUS_SCHEMA, "law_name": "中华人民共和国刑法", "aliases": ["刑法"],
        "version_date": "2020-01-01", "effective_date": None, "version_status": "synthetic",
        "source_url": "https://example.invalid/synthetic-law", "document_id": "synthetic-264",
        "articles": split_articles("第二百六十四条 " + text),
    }
    raw = json.dumps(document, ensure_ascii=False).encode()
    (directory / "law.json").write_bytes(raw)
    (directory / "manifest.json").write_text(json.dumps({
        "schema_version": CORPUS_SCHEMA,
        "documents": [{"document_file": "law.json", "document_sha256": hashlib.sha256(raw).hexdigest()}],
    }))


class HostReadPortTestCase(IsolatedDatabaseTestCase):
    """Shared helpers; concrete classes declare their own seeded setUpClass
    because tests/support.py wraps only the class-dict setUpClass."""
    #: Snapshots of the SQLite file; refused on PostgreSQL (see app.db.SQLITE_FILE_ONLY).
    sqlite_only = "framework snapshot reads the SQLite file"

    @classmethod
    def seed(cls):
        init_db(seed=True)

    def port(self, case_id: int = 1, **kwargs) -> LocalHostReadPort:
        kwargs.setdefault("access_recorder", None)
        return LocalHostReadPort(RunScope(case_id), **kwargs)

    def first_page(self) -> tuple[int, int, str]:
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT p.document_id,p.page_no,p.text FROM pages p JOIN documents d ON d.id=p.document_id "
                "WHERE d.case_id=1 ORDER BY p.id LIMIT 1"
            ).fetchone()
        return int(row["document_id"]), int(row["page_no"]), str(row["text"])


class CatalogReadTests(HostReadPortTestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_list_documents_is_typed_and_matches_the_store(self):
        port = self.port()
        documents = port.list_documents(RunScope(1))
        self.assertTrue(documents)
        self.assertTrue(all(isinstance(item, DocumentRef) for item in documents))
        with closing(connect()) as conn:
            row = conn.execute(
                "SELECT id,name,doc_type,mime_type,pages,status,created_at FROM documents "
                "WHERE case_id=1 ORDER BY id LIMIT 1"
            ).fetchone()
        first = documents[0]
        self.assertEqual(first.document_id, int(row["id"]))
        self.assertEqual(first.name, row["name"])
        self.assertEqual(first.doc_type, row["doc_type"] or "")
        self.assertEqual(first.mime_type, row["mime_type"] or "")
        self.assertEqual(first.page_count, int(row["pages"] or 0))
        self.assertEqual(first.created_at, row["created_at"])

    def test_list_evidence_joins_the_source_document(self):
        port = self.port()
        evidence = port.list_evidence(RunScope(1))
        self.assertTrue(evidence)
        self.assertTrue(all(isinstance(item, EvidenceRef) for item in evidence))
        with closing(connect()) as conn:
            expected = conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE case_id=1"
            ).fetchone()[0]
        self.assertLessEqual(len(evidence), expected)
        sourced = [item for item in evidence if item.source_document_id]
        if sourced:
            self.assertTrue(sourced[0].source_name)

    def test_catalog_limits_are_enforced_not_truncated_silently(self):
        port = self.port()
        limited = port.list_documents(RunScope(1), limit=1)
        self.assertEqual(len(limited), 1)
        for bad in (0, CATALOG_LIMIT_MAX + 1, True, "5"):
            with self.subTest(limit=bad), self.assertRaises(ContractValidationError):
                port.list_documents(RunScope(1), limit=bad)  # type: ignore[arg-type]

    def test_saved_gap_analysis_reads_with_optional_severity(self):
        with transaction() as conn:
            conn.execute(
                """INSERT INTO gap_detections(case_id,run_id,gap_type,severity,description,suggestion,
                                             affected_evidence_ids,created_at)
                   VALUES (1,NULL,'evidence','高','缺少资金流向证据','补充流水',?,?)""",
                (json.dumps([1, 2]), now()),
            )
            conn.execute(
                """INSERT INTO gap_detections(case_id,run_id,gap_type,severity,description,suggestion,
                                             affected_evidence_ids,created_at)
                   VALUES (1,NULL,'statement','低','陈述细节待补','补充笔录','[]',?)""",
                (now(),),
            )
        port = self.port()
        all_gaps = port.saved_gap_analysis(RunScope(1))
        self.assertTrue(all(isinstance(item, GapRecord) for item in all_gaps))
        self.assertEqual([item.severity for item in all_gaps][0], "低")  # ordered by id DESC
        high = port.saved_gap_analysis(RunScope(1), severity="高")
        self.assertEqual(len(high), 1)
        self.assertEqual(high[0].description, "缺少资金流向证据")
        self.assertEqual(high[0].affected_evidence_ids, (1, 2))
        with self.assertRaises(ContractValidationError):
            port.saved_gap_analysis(RunScope(1), severity="紧急")


class SearchReadTests(HostReadPortTestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_search_round_trips_into_domain_contexts(self):
        port = self.port()
        # Compare the full metrics contract, with elapsed time controlled rather
        # than assuming two real executions take the same milliseconds.
        with patch("app.rag.time.perf_counter", return_value=1.0):
            result = port.search(RunScope(1), QUERY, limit=4, use_remote_embeddings=False)
            legacy_items, legacy_metrics = legacy_search(1, {"query": QUERY, "limit": 4}, False)
        self.assertEqual([search_hit_context(hit) for hit in result.hits], legacy_items)
        self.assertEqual(result.metrics, legacy_metrics)
        self.assertTrue(all(hit.quote for hit in result.hits))

    def test_search_bounds_and_remote_capability_are_explicit(self):
        port = self.port()
        with self.assertRaises(ContractValidationError):
            port.search(RunScope(1), "  ", use_remote_embeddings=False)
        with self.assertRaises(ContractValidationError):
            port.search(RunScope(1), "字" * (MAX_QUERY_CHARS + 1), use_remote_embeddings=False)
        with self.assertRaises(ContractValidationError):
            port.search(RunScope(1), QUERY, limit=SEARCH_LIMIT_MAX + 1, use_remote_embeddings=False)
        with self.assertRaises(UnsupportedCapabilityError):
            port.search(RunScope(1), QUERY, use_remote_embeddings=True)

    def test_search_access_is_recorded_without_case_text(self):
        entries: list[tuple[str, dict]] = []
        port = self.port(access_recorder=lambda kind, detail: entries.append((kind, detail)))
        port.search(RunScope(1), QUERY, limit=2, use_remote_embeddings=False)
        self.assertEqual([kind for kind, _ in entries], ["search"])
        detail = entries[0][1]
        self.assertEqual(detail["case_id"], 1)
        self.assertIn("count", detail)
        self.assertNotIn(QUERY, json.dumps(detail, ensure_ascii=False))


class PageReadTests(HostReadPortTestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_read_page_returns_content_identity(self):
        document_id, page_no, text = self.first_page()
        page = self.port().read_page(RunScope(1), document_id, page_no)
        self.assertEqual((page.document_id, page.page_no), (document_id, page_no))
        self.assertEqual(page.text, text)
        self.assertEqual(page.text_sha256, hashlib.sha256(text.encode()).hexdigest())

    def test_missing_or_foreign_page_is_denied(self):
        port = self.port()
        with self.assertRaises(AccessDeniedError):
            port.read_page(RunScope(1), 999999, 1)
        with transaction() as conn:
            other_case = conn.execute(
                "INSERT INTO cases(title,description,created_at,updated_at) VALUES ('隔离','x',?,?)",
                (now(), now()),
            ).lastrowid
            document = conn.execute(
                """INSERT INTO documents(case_id,name,mime_type,pages,status,doc_type,created_at,updated_at)
                   VALUES (?,'外部文件','text/plain',1,'ready','书证',?,?)""",
                (other_case, now(), now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO pages(document_id,page_no,text,summary) VALUES (?,1,'外部案件原文','')",
                (document,),
            )
        with self.assertRaises(AccessDeniedError):
            port.read_page(RunScope(1), int(document), 1)

    def test_oversize_page_is_rejected_not_truncated(self):
        with transaction() as conn:
            document = conn.execute(
                """INSERT INTO documents(case_id,name,mime_type,pages,status,doc_type,created_at,updated_at)
                   VALUES (1,'超长文件','text/plain',1,'ready','书证',?,?)""",
                (now(), now()),
            ).lastrowid
            conn.execute(
                "INSERT INTO pages(document_id,page_no,text,summary) VALUES (?,1,?,'')",
                (document, "长" * 20_001),
            )
        with self.assertRaises(ContractValidationError):
            self.port().read_page(RunScope(1), int(document), 1)
        with self.assertRaises(ContractValidationError):
            self.port().read_page(RunScope(1), 0, 1)


class MemoryReadTests(HostReadPortTestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_recall_memories_is_typed_and_round_trips(self):
        with transaction() as conn:
            run_id = conn.execute(
                """INSERT INTO agent_runs(case_id,question,route,status,runtime,created_at)
                   VALUES (1,'历史问题','语义检索','completed','native',?)""",
                (now(),),
            ).lastrowid
            conn.execute(
                """INSERT INTO memories(case_id,user_name,kind,content,vector_json,embedding_model,
                                        importance,source_run_id,created_at)
                   VALUES (1,'本机律师','agent_draft_validated','历史草稿要点',?,?,0.5,?,?)""",
                (json.dumps(hashed_embedding("历史草稿要点")), "mismatched-space", run_id, now()),
            )
        port = self.port()
        records = port.recall_memories(RunScope(1), "历史草稿要点", limit=3, use_remote_embeddings=False)
        self.assertTrue(records)
        self.assertTrue(all(isinstance(item, MemoryRecord) for item in records))
        matched = [item for item in records if item.content == "历史草稿要点"]
        self.assertTrue(matched)
        context = memory_record_context(matched[0])
        self.assertEqual(context["content"], "历史草稿要点")
        self.assertEqual(context["source_run_id"], run_id)
        self.assertEqual(context["kind"], "agent_draft_validated")
        with self.assertRaises(ContractValidationError):
            port.recall_memories(RunScope(1), "记忆", limit=0, use_remote_embeddings=False)
        with self.assertRaises(UnsupportedCapabilityError):
            port.recall_memories(RunScope(1), "记忆", use_remote_embeddings=True)


class StatuteReadTests(HostReadPortTestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_search_statutes_requires_a_configured_corpus(self):
        with self.assertRaises(UnsupportedCapabilityError):
            self.port().search_statutes(RunScope(1), "刑法第二百六十四条")

    def test_search_statutes_returns_typed_versioned_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_corpus(path)
            corpus = LegalCorpus(path)
            port = self.port(corpus=corpus, corpus_identity="synthetic-corpus-v1")
            hits = port.search_statutes(RunScope(1), "当前受控合成原文", limit=3)
            self.assertTrue(hits)
            hit = hits[0]
            self.assertEqual(hit.law_name, "中华人民共和国刑法")
            self.assertTrue(hit.article_no)
            self.assertIn("当前受控合成原文", hit.text)
            self.assertEqual(hit.corpus_version, "synthetic-corpus-v1")
            self.assertGreaterEqual(hit.score, 0.0)
            self.assertEqual(hit.extra["version_date"], "2020-01-01")

    def test_statutory_lookup_keeps_the_domain_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_corpus(path)
            corpus = LegalCorpus(path)
            port = self.port(corpus=corpus, corpus_identity="synthetic-corpus-v1")
            result = port.statutory_lookup(RunScope(1), "刑法第二百六十四条的法条依据")
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["hits"])


class PortAuthorizationTests(HostReadPortTestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def read_calls(self, port: LocalHostReadPort):
        scope = port.scope
        document_id, page_no, _ = self.first_page()
        return (
            lambda: port.list_documents(scope),
            lambda: port.list_evidence(scope),
            lambda: port.search(scope, QUERY, use_remote_embeddings=False),
            lambda: port.read_page(scope, document_id, page_no),
            lambda: port.recall_memories(scope, QUERY, use_remote_embeddings=False),
            lambda: port.saved_gap_analysis(scope),
            lambda: port.search_statutes(scope, QUERY),
        )

    def test_unauthorized_case_is_denied_for_every_read(self):
        port = self.port()
        with scoped_principal((999,)):
            for call in self.read_calls(port):
                with self.assertRaises(AccessDeniedError):
                    call()

    def test_revocation_between_calls_is_denied(self):
        with scoped_principal((1,)):
            port = self.port()
            self.assertTrue(port.list_documents(RunScope(1)))
        with scoped_principal((), name="受限律师"):
            with self.assertRaises(AccessDeniedError):
                port.list_documents(RunScope(1))

    def test_scope_binding_rejects_another_case(self):
        port = self.port()
        with self.assertRaises(AccessDeniedError):
            port.list_documents(RunScope(2))

    def test_unsupported_tenant_is_rejected_at_construction(self):
        with self.assertRaises(ContractValidationError):
            LocalHostReadPort(RunScope(1, "other-tenant"))
        with self.assertRaises(ContractValidationError):
            LocalHostReadPort(RunScope(0))


class PortVersionCheckTests(HostReadPortTestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_changed_source_fails_closed(self):
        identity = case_identity(capture_case_rows(1))
        port = self.port(expected_source_identity=identity)
        self.assertTrue(port.list_documents(RunScope(1)))
        with transaction() as conn:
            conn.execute("UPDATE pages SET text=text||'（变更）' WHERE id=(SELECT MIN(id) FROM pages)")
        with self.assertRaises(SnapshotChangedError):
            port.list_documents(RunScope(1))
        with self.assertRaises(SnapshotChangedError):
            port.read_page(RunScope(1), *self.first_page()[:2])

    def test_identity_is_stable_and_shared_across_instances(self):
        first = self.port(expected_source_identity=case_identity(capture_case_rows(1)))
        second = self.port(expected_source_identity=first.source_identity())
        self.assertEqual(first.source_identity(), second.source_identity())
        self.assertTrue(second.list_evidence(RunScope(1)))

    def test_schema_mismatch_is_a_contract_error(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale.db"
            with closing(sqlite3.connect(str(path))) as conn:
                conn.execute("PRAGMA user_version = 3")
                conn.commit()
            with self.assertRaises(ValueError):
                capture_case_rows(1, source_path=path)  # the schema gate lives in the capture
            port = self.port(expected_source_identity="unused")
            port.identity_source_path = path
            with self.assertRaises(ContractValidationError):
                port.source_identity()  # the port converts it into a contract error


if __name__ == "__main__":
    unittest.main()
