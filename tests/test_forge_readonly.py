"""Authorized synthetic host data and a real installed framework subprocess."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.db import connect, get_db_path, init_db, now, transaction
from app.forge_bridge import SidecarProtocolError, framework_binding, run_framework
from app.forge_readonly import CitationIntegrityError, ReadonlyCaseSession, SnapshotChangedError, exact_quote
from app.legal_corpus import SCHEMA_VERSION as CORPUS_SCHEMA
from app.legal_corpus import LegalCorpus, split_articles
from app.runtime_adapter import current_actor_identity
from app.runtime_ports import AccessDeniedError, ActorIdentity, ModelConfig, RunRequest, RunScope, new_request_id
from app.security import Principal, _principal
from tests.support import IsolatedDatabaseTestCase


def request(**changes):
    base = RunRequest(request_id=new_request_id(), scope=RunScope(1),
                      actor=current_actor_identity(), question="本案审批陈述是否矛盾并存在证据疏漏？",
                      runtime="forgeharness-readonly",
                      model=ModelConfig(use_llm=False), use_remote_embeddings=False,
                      persist_memory=False)
    return replace(base, **changes)


def dump_source():
    with closing(connect()) as conn:
        return tuple(conn.iterdump())


def write_corpus(directory, text):
    document = {"schema_version": CORPUS_SCHEMA, "law_name": "中华人民共和国刑法", "aliases": ["刑法"],
                "version_date": "2020-01-01", "effective_date": None, "version_status": "synthetic",
                "source_url": "https://example.invalid/synthetic-law", "document_id": "synthetic-264",
                "articles": split_articles("第二百六十四条 " + text)}
    raw = json.dumps(document, ensure_ascii=False).encode()
    (directory / "law.json").write_bytes(raw)
    (directory / "manifest.json").write_text(json.dumps({"schema_version": CORPUS_SCHEMA,
        "documents": [{"document_file": "law.json", "document_sha256": hashlib.sha256(raw).hexdigest()}]}))


def prepare(session):
    session.call("planner")
    session.call("memory_recall")
    session.call("retrieve")
    pending = {item["name"]: item for item in session.outputs["planner"]["nodes"]
               if item["name"] not in {"retrieve", "critic", "memory"}}
    while pending:
        ready = [name for name, item in pending.items() if set(item["depends_on"]) <= session.outputs.keys()]
        if not ready:
            raise AssertionError("no ready node")
        for name in ready:
            session.call(name)
            del pending[name]
    session.call("critic")


class SnapshotHostTests(IsolatedDatabaseTestCase):
    #: Snapshots of the SQLite file; refused on PostgreSQL (see app.db.SQLITE_FILE_ONLY).
    sqlite_only = "framework snapshot reads the SQLite file"
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def test_snapshot_contains_only_the_authorized_case_and_no_source_paths(self):
        with transaction() as conn:
            other = conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('unrelated',?,?)",
                                 (now(), now())).lastrowid
        before = dump_source()
        with ReadonlyCaseSession(request()) as session:
            with closing(sqlite3.connect(session.snapshot)) as conn:
                self.assertEqual(conn.execute("SELECT id FROM cases").fetchall(), [(1,)])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents WHERE stored_path<>''").fetchone()[0], 0)
                for table in ("review_jobs", "auth_sessions", "audit_log", "agent_steps"):
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            directory = session.directory
            prepare(session)
            result = session.call("finish")
            self.assertFalse(result["memory_persisted"])
            self.assertTrue(session.compare_native()["passed"])
            independent = session.compare_native(shared_inputs=False)
            self.assertTrue(independent["passed"], independent)
            self.assertEqual(independent["mode"], "independent_retrieval_and_memory")
            self.assertEqual(before, dump_source())
        self.assertFalse(directory.exists())
        with self.assertRaises(RuntimeError):
            session.call("planner")
        self.assertGreater(other, 1)

    def test_forged_actor_and_unauthorized_case_are_rejected(self):
        with self.assertRaises(AccessDeniedError):
            ReadonlyCaseSession(request(actor=ActorIdentity("forged")))
        token = _principal.set(Principal("limited", False, (999,), local=False, permissions=frozenset({"view"})))
        try:
            with self.assertRaises(AccessDeniedError):
                ReadonlyCaseSession(request())
        finally:
            _principal.reset(token)

    def test_revocation_and_source_change_stop_the_next_node(self):
        principal = Principal("limited", False, (1,), local=False, permissions=frozenset({"view"}))
        token = _principal.set(principal)
        try:
            with ReadonlyCaseSession(request()) as session:
                session.call("planner")
                revoked = _principal.set(replace(principal, case_ids=()))
                try:
                    with self.assertRaises(AccessDeniedError):
                        session.call("memory_recall")
                finally:
                    _principal.reset(revoked)
                with transaction() as conn:
                    conn.execute("UPDATE cases SET updated_at=? WHERE id=1", ("changed-for-test",))
                with self.assertRaises(SnapshotChangedError):
                    session.call("memory_recall")
        finally:
            _principal.reset(token)

    def test_foreign_or_missing_page_is_denied(self):
        with ReadonlyCaseSession(request()) as session:
            with self.assertRaises(AccessDeniedError):
                session.read_page(999999, 1)

    def test_legacy_database_override_cannot_redirect_snapshot_writes_to_source(self):
        before = dump_source()
        with patch("app.db.DB_PATH", get_db_path()):
            with self.assertRaisesRegex(RuntimeError, "safe snapshot isolation"):
                ReadonlyCaseSession(request())
        self.assertEqual(before, dump_source())

    def test_invalid_mode_parameters_and_oversize_pages_fail_explicitly(self):
        variants = [dict(model=ModelConfig(use_llm=True)), dict(use_remote_embeddings=True),
                    dict(persist_memory=True), dict(scope=RunScope(1, "other-tenant")),
                    dict(question="字" * 501), dict(model=ModelConfig(use_llm=False, model="ignored"))]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                ReadonlyCaseSession(request(**variant))
        with self.assertRaisesRegex(ValueError, "snapshot bounds"):
            ReadonlyCaseSession(request(), max_pages=1)

    def test_untrusted_node_names_and_dependency_bypass_are_rejected(self):
        before = dump_source()
        with ReadonlyCaseSession(request()) as session:
            for node in ("__import__", "critic", "finish", "SELECT * FROM cases"):
                with self.subTest(node=node), self.assertRaises(ValueError):
                    session.call(node)
            session.call("planner")
            with self.assertRaises(ValueError):
                session.call("planner")
        self.assertEqual(before, dump_source())

    def test_stale_retrieval_quote_cannot_be_replaced_with_an_unrelated_span(self):
        with ReadonlyCaseSession(request()) as session:
            prepare(session)
            session.outputs["retrieve"]["contexts"][0]["quote"] = "fabricated quote not in any page"
            with self.assertRaises(CitationIntegrityError):
                session.call("finish")

    def test_statute_artifact_is_pinned_independently_of_the_legacy_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_corpus(path, "旧缓存文字。")
            stale = LegalCorpus(path)
            write_corpus(path, "当前受控合成原文。")
            with patch.dict(os.environ, {"LAW_REVIEW_LEGAL_CORPUS_DIR": directory}):
                with patch("app.statutory_retrieval._corpus_cache", (directory, stale)):
                    with ReadonlyCaseSession(request(question="刑法第二百六十四条的法条与固定回报审批")) as session:
                        prepare(session)
                        output = session.outputs["statutory_conflict"]
                        self.assertEqual(output["status"], "ok")
                        self.assertIn("当前受控合成原文", output["statutes"][0]["text"])
                        session.call("finish")
                        self.assertTrue(session.compare_native()["passed"])
                        (path / "law.json").write_text("tampered")
                        with self.assertRaises(SnapshotChangedError):
                            session.assert_current()

    def test_exact_quote_preserves_original_whitespace_and_drops_display_ellipsis(self):
        self.assertEqual(exact_quote("原文甲\n\t乙丙丁", "…甲 乙丙…"), "甲\n\t乙丙")
        for display in ("", "…", "unrelated"):
            with self.assertRaises(CitationIntegrityError):
                exact_quote("原文", display)


FRAMEWORK_PYTHON = os.getenv("LAW_REVIEW_FORGE_PYTHON", "")


@unittest.skipUnless(FRAMEWORK_PYTHON, "set LAW_REVIEW_FORGE_PYTHON to the separately installed Python 3.12 sidecar")
class FrameworkProcessTests(IsolatedDatabaseTestCase):
    #: Snapshots of the SQLite file; refused on PostgreSQL (see app.db.SQLITE_FILE_ONLY).
    sqlite_only = "framework snapshot reads the SQLite file"
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def run_pilot(self, question, expected_status="succeeded"):
        before = dump_source()
        with ReadonlyCaseSession(request(question=question)) as session:
            with patch("app.agents.call_local_llm", side_effect=AssertionError("model call forbidden")):
                report = run_framework(session, Path(FRAMEWORK_PYTHON))
            self.assertEqual(report["status"], expected_status, report)
            self.assertEqual(report["tool_calls"], len(session.access_log))
            self.assertEqual(report["tool_calls"], len(report["framework_tool_evidence"]))
            self.assertTrue(report["framework_trace"])
            self.assertTrue(any(item.get("role") == "user" for item in report["framework_messages"]))
            self.assertTrue(report["comparison"]["passed"])
            self.assertFalse(report["business_qualified"])
            self.assertFalse(report["resumable"])
        self.assertEqual(before, dump_source())
        return report

    def test_eight_synthetic_questions_through_real_sdk_and_original_experts(self):
        for question, expected in (
            ("核对本案事实与时间线", "succeeded"), ("检查证据来源和证明力", "succeeded"),
            ("张某关于固定回报审批的陈述是否矛盾？", "succeeded"),
            ("本案证据链有哪些疏漏和缺失？", "succeeded"),
            ("刑法第一百七十六条的法律规定是什么？", "failed"),
            ("核对银行流水与资金流向", "succeeded"), ("询问笔录与电子数据是否一致？", "succeeded"),
            ("检查审批陈述矛盾、证据疏漏和相关法条", "succeeded"),
        ):
            with self.subTest(question=question):
                report = self.run_pilot(question, expected)
                if expected == "succeeded":
                    self.assertTrue(report["result"]["citations"])
                else:
                    self.assertFalse(report["result"]["citations"])
                    self.assertIn("evidence is insufficient", report["error"])

    def test_case_over_100_pages_uses_retrieved_page_bundle_without_truncating_snapshot(self):
        with transaction() as conn:
            document = conn.execute("SELECT id FROM documents WHERE case_id=1 LIMIT 1").fetchone()[0]
            for number in range(1000, 1126):
                conn.execute("INSERT INTO pages(document_id,page_no,text,summary) VALUES (?,?,?, '')",
                             (document, number, f"合成规模测试：第{number}页固定回报审批记录。"))
        report = self.run_pilot("核对固定回报审批")
        identities = {(item["document_id"], item["page"]) for item in report["result"]["citations"]}
        self.assertEqual(len(identities), len(report["result"]["citations"]))

    def test_no_source_pages_is_reported_as_failure_not_verified_success(self):
        with transaction() as conn:
            case_id = conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('empty',?,?)",
                                   (now(), now())).lastrowid
        with ReadonlyCaseSession(request(scope=RunScope(case_id))) as session:
            report = run_framework(session, Path(FRAMEWORK_PYTHON))
            self.assertEqual(report["status"], "failed")
            self.assertIn("insufficient", report["error"])

    def test_configured_statute_port_runs_through_the_real_framework(self):
        with tempfile.TemporaryDirectory() as directory:
            write_corpus(Path(directory), "固定回报审批测试条文，仅为合成测试。")
            with patch.dict(os.environ, {"LAW_REVIEW_LEGAL_CORPUS_DIR": directory}):
                before = dump_source()
                with ReadonlyCaseSession(request(question="刑法第二百六十四条的法条与固定回报审批")) as session:
                    report = run_framework(session, Path(FRAMEWORK_PYTHON))
                    self.assertEqual(report["status"], "succeeded", report)
                    self.assertEqual(session.outputs["statutory_conflict"]["status"], "ok")
                    self.assertTrue(report["comparison"]["passed"])
                self.assertEqual(before, dump_source())

    def test_deadline_and_host_denial_terminate_the_sidecar(self):
        with ReadonlyCaseSession(request()) as session:
            with self.assertRaises(TimeoutError):
                run_framework(session, Path(FRAMEWORK_PYTHON), timeout_seconds=0.0001)
        with ReadonlyCaseSession(request()) as session:
            with patch.object(session, "call", side_effect=AccessDeniedError("revoked")):
                with self.assertRaises(AccessDeniedError):
                    run_framework(session, Path(FRAMEWORK_PYTHON))


    def test_wrong_token_sequence_and_premature_success_never_execute_host_nodes(self):
        programs = [
            "{'type':'node','node':'planner','seq':1,'token':'wrong'}",
            "{'type':'node','node':'planner','seq':2}",
            "{'type':'result','version':i['version'],'sdk_api':1,'artifact_verified':True,'trace_valid':True,'status':'succeeded'}",
        ]
        for message in programs:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                worker = Path(directory) / "app/forge_worker.py"
                worker.parent.mkdir()
                worker.write_text("import sys,json\ni=json.loads(sys.stdin.readline())\n"
                                  f"m={message}\nm.setdefault('token',i['token'])\n"
                                  "m['wire_version']=1\nprint(json.dumps(m),flush=True)\n")
                with ReadonlyCaseSession(request()) as session:
                    with patch("app.forge_bridge.ROOT", Path(directory)):
                        with self.assertRaises(SidecarProtocolError):
                            run_framework(session, Path(FRAMEWORK_PYTHON))
                    self.assertEqual(session.outputs, {})


class BindingTests(unittest.TestCase):
    def test_wheel_mismatch_is_rejected_before_starting_a_process(self):
        binding, wheel = framework_binding()
        self.assertEqual(binding["version_label"], "0.8.3")
        with patch("app.forge_bridge.hashlib.sha256") as sha:
            sha.return_value.hexdigest.return_value = "0" * 64
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                framework_binding()
        self.assertTrue(wheel.is_file())
