"""Behaviour tests for LegacyRuntimeControl: the M1 compatibility interface.

Covers the frozen contract guarantees on top of the untouched legacy runtime:
idempotent start with durable request<->run mapping, fail-closed replay when
the association is unknown, per-call authorization re-checks, event
projection, LangGraph resume capability boundaries, and job status parity
with the existing polling route. All runs are offline (use_llm=False,
hashed-local embeddings) on class-isolated databases.
"""
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from unittest.mock import patch

TEST_DATA = tempfile.mkdtemp(prefix="lexvault-runtime-adapter-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA

from app.core.security import Principal, _principal, current_principal  # noqa: E402
from app.integration.runtime_adapter import LegacyRuntimeControl, current_actor_identity  # noqa: E402
from app.integration.runtime_ports import (  # noqa: E402
    EXECUTION_MODE_BACKGROUND,
    RUNTIME_PROTOCOL_VERSION,
    AccessDeniedError,
    ActorIdentity,
    ContractValidationError,
    IdentityMismatchError,
    ModelConfig,
    ProtocolVersionError,
    ReconciliationRequiredError,
    RequestConflictError,
    ResumeConflictError,
    ResumeNotSupportedError,
    RunNotFoundError,
    RunRequest,
    RunScope,
    UnsupportedExecutionModeError,
    new_request_id,
)
from app.review.langgraph_agents import LangGraphRunError, create_langgraph_coordinator  # noqa: E402
from app.storage.db import connect, init_db, now, transaction  # noqa: E402
from tests.support import IsolatedDatabaseTestCase  # noqa: E402

QUESTION = "张某关于固定回报审批的陈述是否矛盾？"


def make_request(**overrides) -> RunRequest:
    """Build a request whose actor mirrors the LIVE authenticated principal.

    The adapter verifies the assertion (A2), so tests must construct the
    actor from the auth context instead of hand-writing a claim.
    """
    base = dict(
        request_id=new_request_id(),
        scope=RunScope(case_id=1),
        actor=current_actor_identity(),
        question=QUESTION,
        runtime="native",
        use_remote_embeddings=False,
        model=ModelConfig(use_llm=False),
    )
    base.update(overrides)
    return RunRequest(**base)


@contextmanager
def scoped_principal(case_ids, name="受限律师", admin=False, local=False, permissions=("view",)):
    marker = _principal.set(
        Principal(name, admin, tuple(case_ids), local=local, permissions=frozenset(permissions))
    )
    try:
        yield
    finally:
        _principal.reset(marker)


class LegacyRuntimeControlTestBase(IsolatedDatabaseTestCase):
    """Shared helpers; concrete classes declare their own seeded setUpClass
    because tests/support.py wraps only the class-dict setUpClass."""

    @classmethod
    def seed(cls):
        init_db(seed=True)

    def setUp(self):
        self.control = LegacyRuntimeControl()

    def run_count(self) -> int:
        with closing(connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]

    def mapping_row(self, request_id: str):
        with closing(connect()) as conn:
            return conn.execute(
                "SELECT * FROM runtime_requests WHERE request_id=?", (request_id,)
            ).fetchone()

    def failed_langgraph_run(self) -> int:
        """Create a failed-but-resumable LangGraph run (memory node interrupted)."""
        def failure(node, state):
            if node == "memory":
                raise RuntimeError("test interruption before memory commit")

        coordinator = create_langgraph_coordinator(1, False, failure_injector=failure)
        with self.assertRaises(LangGraphRunError) as caught:
            coordinator.process_query("检查本案证据疏漏", use_llm=False)
        return caught.exception.run_id

    def table_count(self, table: str) -> int:
        with closing(connect()) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class StartContractTests(LegacyRuntimeControlTestBase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_native_start_completes_and_maps_request(self):
        request = make_request()
        handle = self.control.start(request)
        self.assertEqual(handle.status, "completed")
        self.assertTrue(handle.started_new)
        self.assertIsNotNone(handle.run_id)
        self.assertIsNotNone(handle.result)
        legacy = handle.result.to_legacy()
        self.assertEqual(legacy["agent_type"], "DAG Multi-Agent")
        self.assertEqual(legacy["runtime"], "native")
        self.assertFalse(legacy["resumable"])
        self.assertTrue(legacy["citations"])

        row = self.mapping_row(request.request_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["run_id"], handle.run_id)
        self.assertEqual(row["runtime"], "native")
        self.assertEqual(row["protocol_version"], RUNTIME_PROTOCOL_VERSION)
        self.assertEqual(row["case_id"], 1)
        self.assertEqual(row["principal_name"], current_principal().name)
        self.assertRegex(row["request_digest"], r"^[a-f0-9]{64}$")

        with closing(connect()) as conn:
            run = conn.execute("SELECT status, runtime FROM agent_runs WHERE id=?", (handle.run_id,)).fetchone()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["runtime"], "native")

    def test_langgraph_start_records_checkpoint_identity(self):
        handle = self.control.start(make_request(runtime="langgraph"))
        self.assertEqual(handle.status, "completed")
        self.assertEqual(handle.result.runtime, "langgraph")
        self.assertEqual(handle.result.extra["agent_type"], "LangGraph Multi-Agent")
        self.assertEqual(handle.result.checkpoint_thread_id, f"agent-run-{handle.run_id}")

    def test_retry_same_request_never_creates_second_run(self):
        request = make_request()
        first = self.control.start(request)
        before = self.run_count()
        second = self.control.start(request)
        self.assertFalse(second.started_new)
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(second.status, "completed")
        self.assertIsNone(second.result)  # replay reports the association; inspect() projects details
        self.assertEqual(self.run_count(), before)

    def test_unknown_association_fails_closed(self):
        request = make_request()
        before = self.run_count()
        with transaction() as conn:
            conn.execute(
                """INSERT INTO runtime_requests(request_id,case_id,runtime,protocol_version,created_at)
                   VALUES (?,?,?,1,?)""",
                (request.request_id, 1, "native", now()),
            )
        with self.assertRaises(ReconciliationRequiredError):
            self.control.start(request)
        self.assertEqual(self.run_count(), before)

    def test_concurrent_same_request_executes_once(self):
        request = make_request()
        before = self.run_count()
        outcomes = []

        def attempt():
            try:
                return ("handle", self.control.start(request))
            except Exception as exc:  # noqa: BLE001 - assertions below classify
                return ("error", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        started = [value for kind, value in outcomes if kind == "handle" and value.started_new]
        self.assertEqual(len(started), 1, f"exactly one execution must win: {outcomes}")
        self.assertEqual(started[0].status, "completed")
        for kind, value in outcomes:
            if kind == "handle" and not value.started_new:
                self.assertEqual(value.run_id, started[0].run_id)
            elif kind == "error":
                self.assertIsInstance(value, ReconciliationRequiredError)
        self.assertEqual(self.run_count(), before + 1)

    def test_rejected_requests_leave_no_mapping(self):
        before = self.run_count()
        with closing(connect()) as conn:
            mappings_before = conn.execute("SELECT COUNT(*) FROM runtime_requests").fetchone()[0]
        with self.assertRaises(ProtocolVersionError):
            self.control.start(make_request(protocol_version=99))
        with self.assertRaises(ContractValidationError):
            self.control.start(make_request(request_id="_bad"))
        with self.assertRaises(UnsupportedExecutionModeError):
            self.control.start(make_request(execution_mode=EXECUTION_MODE_BACKGROUND))
        with self.assertRaises(ContractValidationError):
            self.control.start(make_request(runtime="forgeharness"))
        self.assertEqual(self.run_count(), before)
        with closing(connect()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM runtime_requests").fetchone()[0], mappings_before
            )

    def test_unauthorized_case_is_denied_before_execution(self):
        before = self.run_count()
        with scoped_principal((999,)):
            with self.assertRaises(AccessDeniedError):
                self.control.start(make_request())
        self.assertEqual(self.run_count(), before)

    def test_execution_failure_without_callback_keeps_claim_for_reconciliation(self):
        class ExplodingCoordinator:
            def process_query(self, *args, **kwargs):
                raise RuntimeError("boom before any run row")

        request = make_request()
        before = self.run_count()
        with patch("app.review.agents.create_coordinator", return_value=ExplodingCoordinator()):
            handle = self.control.start(request)
        self.assertEqual(handle.status, "failed")
        self.assertTrue(handle.started_new)
        self.assertIsNone(handle.run_id)
        self.assertEqual(handle.error.code, "runtime_adapter_failed")
        self.assertEqual(handle.error.diagnostic["error_type"], "RuntimeError")
        self.assertFalse(handle.error.resumable)
        self.assertIsNotNone(self.mapping_row(request.request_id))
        self.assertTrue(handle.needs_reconciliation)
        with self.assertRaises(ReconciliationRequiredError):
            self.control.start(request)
        self.assertEqual(self.run_count(), before)  # verified zero side effect

    def test_constructor_failure_before_execution_releases_the_claim(self):
        request = make_request()
        before = self.run_count()
        with patch("app.review.agents.create_coordinator", side_effect=RuntimeError("initialization failed")):
            handle = self.control.start(request)
        self.assertEqual(handle.status, "failed")
        self.assertFalse(handle.needs_reconciliation)
        self.assertIsNone(self.mapping_row(request.request_id))
        retry = self.control.start(request)
        self.assertEqual(retry.status, "completed")
        self.assertTrue(retry.started_new)
        self.assertEqual(self.run_count(), before + 1)

    def test_run_commit_before_missing_callback_never_reexecutes(self):
        class LostCallbackCoordinator:
            def process_query(self, *args, **kwargs):
                with transaction() as conn:
                    conn.execute(
                        "INSERT INTO agent_runs(case_id,question,route,status,runtime,created_at) "
                        "VALUES (1,?,'test','running','native',?)", (QUESTION, now()))
                raise RuntimeError("commit succeeded but callback was not delivered")

        request = make_request()
        before = self.run_count()
        with patch("app.review.agents.create_coordinator", return_value=LostCallbackCoordinator()) as factory:
            handle = self.control.start(request)
            self.assertTrue(handle.needs_reconciliation)
            with self.assertRaises(ReconciliationRequiredError):
                self.control.start(request)
            self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.run_count(), before + 1)

    def test_langgraph_start_failure_stays_resumable_and_mapped(self):
        def failure(node, state):
            if node == "memory":
                raise RuntimeError("test interruption before memory commit")

        real_factory = create_langgraph_coordinator

        def injecting_factory(case_id, prefer_remote, on_run_started=None):
            return real_factory(case_id, prefer_remote, failure_injector=failure,
                                on_run_started=on_run_started)

        request = make_request(runtime="langgraph")
        with patch("app.review.langgraph_agents.create_langgraph_coordinator", side_effect=injecting_factory):
            handle = self.control.start(request)
        self.assertEqual(handle.status, "failed")
        self.assertIsNotNone(handle.run_id)
        self.assertTrue(handle.error.resumable)
        self.assertEqual(handle.error.runtime, "langgraph")
        self.assertEqual(handle.error.checkpoint_thread_id, f"agent-run-{handle.run_id}")
        row = self.mapping_row(request.request_id)
        self.assertEqual(row["run_id"], handle.run_id)  # claim stays linked; no blind replay

        resumed = self.control.resume(handle.run_id)
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.request_id, request.request_id)
        self.assertEqual(resumed.result.resume_count, 1)


class InspectProjectionTests(LegacyRuntimeControlTestBase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_inspect_projects_ordered_events(self):
        handle = self.control.start(make_request())
        trace = self.control.inspect(handle.run_id)
        self.assertEqual(trace.status, "completed")
        self.assertEqual(trace.runtime, "native")
        self.assertFalse(trace.resumable)
        self.assertTrue(trace.events)
        seqs = [event.seq for event in trace.events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        node_ids = {event.node_id for event in trace.events}
        self.assertTrue({"planner", "retrieve", "critic"} <= node_ids)
        self.assertEqual(len(trace.raw_steps), len(trace.events))

    def test_inspect_unknown_run_raises_not_found(self):
        with self.assertRaises(RunNotFoundError):
            self.control.inspect(424242)

    def test_inspect_rechecks_current_authorization(self):
        handle = self.control.start(make_request())
        with scoped_principal((999,)):
            with self.assertRaises(AccessDeniedError):
                self.control.inspect(handle.run_id)


class ResumeCapabilityTests(LegacyRuntimeControlTestBase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_native_run_resume_is_declared_unsupported(self):
        handle = self.control.start(make_request())
        with self.assertRaises(ResumeNotSupportedError):
            self.control.resume(handle.run_id)

    def test_langgraph_failure_then_resume_completes(self):
        run_id = self.failed_langgraph_run()
        handle = self.control.resume(run_id)
        self.assertEqual(handle.status, "completed")
        self.assertFalse(handle.started_new)
        self.assertEqual(handle.request_id, "")  # legacy run without request mapping
        self.assertEqual(handle.result.runtime, "langgraph")
        self.assertEqual(handle.result.resume_count, 1)
        with closing(connect()) as conn:
            row = conn.execute("SELECT status, resume_count FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["resume_count"], 1)

    def test_resume_maps_request_id_when_mapping_exists(self):
        request = make_request(runtime="langgraph")
        started = self.control.start(request)
        self.assertEqual(started.status, "completed")
        # A completed run cannot resume: the conflict must be reported honestly.
        with self.assertRaises(ResumeConflictError):
            self.control.resume(started.run_id)

    def test_resume_unknown_run_raises_not_found(self):
        with self.assertRaises(RunNotFoundError):
            self.control.resume(424242)

    def test_resume_denied_for_unauthorized_case(self):
        run_id = self.failed_langgraph_run()
        with scoped_principal((999,)):
            with self.assertRaises(AccessDeniedError):
                self.control.resume(run_id)

    def test_resume_failure_reports_resumable_error(self):
        run_id = self.failed_langgraph_run()
        error = LangGraphRunError("langgraph_resume_failed", run_id, True, 1)
        with patch("app.review.langgraph_agents.resume_langgraph_run", side_effect=error):
            handle = self.control.resume(run_id)
        self.assertEqual(handle.status, "failed")
        self.assertFalse(handle.started_new)
        self.assertTrue(handle.error.resumable)
        self.assertEqual(handle.error.resume_count, 1)

    def test_resume_lost_race_maps_to_not_found(self):
        run_id = self.failed_langgraph_run()
        with patch("app.review.langgraph_agents.resume_langgraph_run", side_effect=KeyError("gone")):
            with self.assertRaises(RunNotFoundError):
                self.control.resume(run_id)

    def test_factory_exposes_runtime_control(self):
        from app.integration.runtime_adapter import legacy_runtime_control
        self.assertIsInstance(legacy_runtime_control(), LegacyRuntimeControl)


class JobStatusProjectionTests(LegacyRuntimeControlTestBase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def insert_job(self, status: str, *, result_payload=None, error_payload=None, mode="multi_agent") -> str:
        job_id = new_request_id()
        with transaction() as conn:
            conn.execute(
                "INSERT INTO review_jobs(id,case_id,principal,status,request_json,created_at) VALUES (?,?,?,'queued',?,?)",
                (job_id, 1, "本机律师", json.dumps({"mode": mode, "question": QUESTION}), now()),
            )
            conn.execute(
                "UPDATE review_jobs SET status=?, result_json=?, error_json=? WHERE id=?",
                (status,
                 json.dumps(result_payload, ensure_ascii=False) if result_payload else "{}",
                 json.dumps(error_payload, ensure_ascii=False) if error_payload else "{}",
                 job_id),
            )
        return job_id

    def test_completed_job_projects_result(self):
        payload = {"run_id": None, "answer": "答案 [资料1]", "route": "语义检索", "runtime": "native",
                   "citations": [], "steps": [], "plan": [], "retrieval_metrics": {}, "llm_used": False,
                   "llm_model": "", "fallback_reason": None, "validation": None, "citation_check": "",
                   "resumable": False, "resume_count": 0, "checkpoint_thread_id": "", "total_ms": 5,
                   "memory_persisted": False, "conversation_id": 11}
        job_id = self.insert_job("completed", result_payload=payload)
        status = self.control.status(job_id)
        self.assertEqual(status.status, "completed")
        self.assertEqual(status.runtime, "native")
        self.assertIsNotNone(status.result)
        self.assertEqual(status.result.extra["conversation_id"], 11)
        self.assertFalse(status.resumable)

    def test_interrupted_job_synthesizes_restart_error(self):
        job_id = self.insert_job("interrupted", mode="langgraph")
        status = self.control.status(job_id)
        self.assertEqual(status.runtime, "langgraph")
        self.assertEqual(status.error["type"], "Interrupted")
        self.assertIn("LangGraph", status.error["message"])

    def test_failed_job_keeps_stored_error(self):
        job_id = self.insert_job("failed", error_payload={"type": "RuntimeError", "message": "后台阅卷执行失败"})
        status = self.control.status(job_id)
        self.assertEqual(status.status, "failed")
        self.assertEqual(status.error["type"], "RuntimeError")

    def test_unknown_job_raises_not_found(self):
        with self.assertRaises(RunNotFoundError):
            self.control.status("missing-job")

    def test_job_status_rechecks_authorization(self):
        job_id = self.insert_job("completed", result_payload={"answer": "x"})
        with scoped_principal((999,)):
            with self.assertRaises(AccessDeniedError):
                self.control.status(job_id)


class RequestBindingTests(LegacyRuntimeControlTestBase):
    """A1 acceptance: a request_id replays ONLY the byte-identical request."""

    @classmethod
    def setUpClass(cls):
        cls.seed()

    def assertNoLeak(self, error: Exception, first_handle) -> None:
        message = str(error).replace(first_handle.request_id, "")
        self.assertNotIn(str(first_handle.run_id), message)
        self.assertNotIn("completed", message)
        self.assertNotIn("case_id", message)

    def test_same_key_different_case_conflicts_without_leak(self):
        first = self.control.start(make_request())
        before = self.run_count()
        with scoped_principal((999,)):
            replay = make_request(request_id=first.request_id, scope=RunScope(case_id=999))
            with self.assertRaises(RequestConflictError) as caught:
                self.control.start(replay)
        self.assertNoLeak(caught.exception, first)
        self.assertEqual(self.run_count(), before)

    def test_same_key_changed_execution_fields_conflict(self):
        first = self.control.start(make_request())
        before = self.run_count()
        variants = {
            "question": dict(question="另一个问题：资金流向如何？"),
            "runtime": dict(runtime="langgraph"),
            "use_llm": dict(model=ModelConfig(use_llm=True)),
            "persist_memory": dict(persist_memory=False),
            "author": dict(user_name="另一位作者"),
            "embeddings": dict(use_remote_embeddings=True),
        }
        for label, overrides in variants.items():
            with self.subTest(changed=label):
                with self.assertRaises(RequestConflictError):
                    self.control.start(make_request(request_id=first.request_id, **overrides))
        self.assertEqual(self.run_count(), before)

    def test_same_key_different_principal_conflicts(self):
        with scoped_principal((1,), name="甲律师"):
            first = self.control.start(make_request())
            request_id = first.request_id
        with scoped_principal((1,), name="乙律师"):
            with self.assertRaises(RequestConflictError):
                self.control.start(make_request(request_id=request_id))

    def test_identical_retry_still_replays(self):
        request = make_request()
        first = self.control.start(request)
        second = self.control.start(make_request(
            request_id=request.request_id, question=f"  {request.question}  ",  # whitespace-normalized
        ))
        self.assertFalse(second.started_new)
        self.assertEqual(second.run_id, first.run_id)

    def test_identical_retry_reverifies_mapped_run_authorization(self):
        # Corrupted/stale mapping defense: even for a byte-identical request,
        # the run actually linked must belong to a case the caller may access.
        with scoped_principal((1,), name="丙律师"):
            first = self.control.start(make_request())
            with transaction() as conn:
                other_case = conn.execute(
                    "INSERT INTO cases(title,description,created_at,updated_at) VALUES (?,?,?,?)",
                    ("隔离案件", "另一案件", now(), now()),
                ).lastrowid
                foreign_run = conn.execute(
                    """INSERT INTO agent_runs(case_id,question,route,status,runtime,created_at)
                       VALUES (?,'外部问题','语义检索','completed','native',?)""",
                    (other_case, now()),
                ).lastrowid
                conn.execute(
                    "UPDATE runtime_requests SET run_id=? WHERE request_id=?",
                    (foreign_run, first.request_id),
                )
            with self.assertRaises(AccessDeniedError):
                self.control.start(make_request(request_id=first.request_id))

    def test_revoked_access_is_denied_before_replay(self):
        with scoped_principal((1,), name="丁律师"):
            first = self.control.start(make_request())
            request_id = first.request_id
        with scoped_principal((), name="丁律师"):  # same identity, case access revoked
            with self.assertRaises(AccessDeniedError):
                self.control.start(make_request(request_id=request_id))

    def test_authorized_but_inconsistent_run_mapping_requires_reconciliation(self):
        request = make_request()
        first = self.control.start(request)
        with transaction() as conn:
            conn.execute("UPDATE agent_runs SET question='different request' WHERE id=?", (first.run_id,))
        with self.assertRaisesRegex(ReconciliationRequiredError, "inconsistent"):
            self.control.start(request)

    def test_crash_window_mapping_requires_reconciliation(self):
        # A claim whose digest landed but whose run association never did
        # (crash between run insert and mapping update) must fail closed.
        from app.integration.runtime_ports import compute_request_digest

        request = make_request()
        digest = compute_request_digest(request, principal_name="本机律师", effective_author="本机律师")
        with transaction() as conn:
            conn.execute(
                """INSERT INTO runtime_requests
                   (request_id,case_id,runtime,protocol_version,created_at,request_digest,principal_name)
                   VALUES (?,?,?,2,?,?,?)""",
                (request.request_id, 1, "native", now(), digest, "本机律师"),
            )
        before = self.run_count()
        with self.assertRaises(ReconciliationRequiredError) as caught:
            self.control.start(request)
        self.assertIn("association is unknown", str(caught.exception))
        self.assertEqual(self.run_count(), before)

    def test_legacy_row_without_digest_never_replays(self):
        request = make_request()
        with transaction() as conn:
            stale_run = conn.execute(
                """INSERT INTO agent_runs(case_id,question,route,status,runtime,created_at)
                   VALUES (1,'历史问题','语义检索','completed','native',?)""",
                (now(),),
            ).lastrowid
            conn.execute(
                """INSERT INTO runtime_requests(request_id,case_id,runtime,protocol_version,run_id,created_at)
                   VALUES (?,?,?,1,?,?)""",
                (request.request_id, 1, "native", stale_run, now()),
            )
        before = self.run_count()  # counted after the fixture insert
        with self.assertRaises(ReconciliationRequiredError) as caught:
            self.control.start(request)
        message = str(caught.exception)
        self.assertIn("reconciliation", message)
        self.assertNotIn(str(stale_run), message.replace(request.request_id, ""))  # no disclosure
        self.assertEqual(self.run_count(), before)  # nothing executed


class TrustedIdentityTests(LegacyRuntimeControlTestBase):
    """A2 acceptance: identity is verified against the auth context."""

    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_forged_actor_name_is_rejected(self):
        before = self.run_count()
        with self.assertRaises(IdentityMismatchError):
            self.control.start(make_request(actor=ActorIdentity("冒充者")))
        self.assertEqual(self.run_count(), before)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runtime_requests").fetchone()[0], 0)

    def test_privilege_escalation_flags_are_rejected(self):
        principal = current_principal()  # local admin
        with self.assertRaises(IdentityMismatchError):
            self.control.start(make_request(
                actor=ActorIdentity(principal.name, admin=False, local=principal.local)
            ))
        with scoped_principal((1,)):
            with self.assertRaises(IdentityMismatchError):
                self.control.start(make_request(
                    actor=ActorIdentity("受限律师", admin=True, local=False)
                ))

    def test_unsupported_tenant_is_explicitly_rejected(self):
        with self.assertRaises(ContractValidationError) as caught:
            self.control.start(make_request(scope=RunScope(case_id=1, tenant_id="other-firm")))
        self.assertIn("tenant", str(caught.exception))

    def test_missing_principal_in_token_mode_is_denied(self):
        before = self.run_count()
        environment = patch.dict(os.environ, {
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
        })
        with environment:
            # No _principal bound: background identity loss must deny, never
            # fall back to an anonymous local identity.
            with self.assertRaises(AccessDeniedError):
                current_actor_identity()
            request = RunRequest(
                request_id=new_request_id(),
                scope=RunScope(case_id=1),
                actor=ActorIdentity("本机律师", admin=True, local=True),
                question=QUESTION,
                use_remote_embeddings=False,
                model=ModelConfig(use_llm=False),
            )
            with self.assertRaises(AccessDeniedError):
                self.control.start(request)
        self.assertEqual(self.run_count(), before)

    def test_token_mode_author_is_normalized_to_principal(self):
        token = "t" * 40
        environment = patch.dict(os.environ, {
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({token: {"name": "案件一律师", "case_ids": [1]}}),
        })
        with environment, scoped_principal((1,), name="案件一律师"):
            request = make_request(user_name="客户端展示名")
            self.assertEqual(request.actor.name, "案件一律师")
            handle = self.control.start(request)
        self.assertEqual(handle.status, "completed", handle.error)
        with closing(connect()) as conn:
            authors = [row["user_name"] for row in conn.execute(
                "SELECT user_name FROM memories WHERE source_run_id=?", (handle.run_id,)
            )]
        self.assertTrue(authors, "the offline fallback path persists a draft memory")
        self.assertEqual(authors, ["案件一律师"] * len(authors))  # display name never becomes the author


class TraceRevisionTests(LegacyRuntimeControlTestBase):
    """A3 acceptance: snapshot + revision semantics over mutable step rows."""

    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_resume_updates_are_visible_through_revision_change(self):
        run_id = self.failed_langgraph_run()
        first = self.control.inspect(run_id)
        self.assertEqual(first.status, "failed")
        self.assertRegex(first.trace_revision, r"^[a-f0-9]{64}$")
        memory_before = next(e for e in first.events if e.node_id == "memory")
        self.assertEqual(memory_before.status, "failed")
        self.assertEqual(memory_before.event_version, 2)

        # Re-read after a "restart" (fresh control instance) is deterministic.
        reread = LegacyRuntimeControl().inspect(run_id)
        self.assertEqual(reread.trace_revision, first.trace_revision)
        self.assertEqual(reread.events, first.events)

        runs_before = self.table_count("agent_runs")
        handle = self.control.resume(run_id)
        self.assertEqual(handle.status, "completed")

        second = self.control.inspect(run_id)
        self.assertEqual(second.status, "completed")
        self.assertNotEqual(second.trace_revision, first.trace_revision)
        self.assertEqual(
            [(e.seq, e.node_id) for e in first.events],
            [(e.seq, e.node_id) for e in second.events],
        )
        memory_after = next(e for e in second.events if e.node_id == "memory")
        self.assertEqual(memory_after.status, "completed")

        # A revision-keyed consumer ends at the completed projection, and the
        # resume produced no duplicate business side effects.
        cache = {}
        for trace in (first, reread, second):
            cache[(trace.run_id, trace.trace_revision)] = {e.seq: e.status for e in trace.events}
        self.assertEqual(len(cache), 2)
        self.assertTrue(all(s == "completed" for s in cache[(second.run_id, second.trace_revision)].values()))
        # No duplicate business side effects from fail->resume: exactly one run
        # row, at most one draft memory per source run, and gap rows unique on
        # (run_id, gap_type, description).
        self.assertEqual(self.table_count("agent_runs"), runs_before)
        with closing(connect()) as conn:
            memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_run_id=?", (run_id,)
            ).fetchone()[0]
            gaps_total = conn.execute(
                "SELECT COUNT(*) FROM gap_detections WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            gaps_distinct = conn.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT gap_type, description FROM gap_detections WHERE run_id=?)",
                (run_id,),
            ).fetchone()[0]
        self.assertLessEqual(memories, 1)
        self.assertEqual(gaps_total, gaps_distinct)

    def test_native_completed_trace_revision_is_stable(self):
        handle = self.control.start(make_request())
        a = self.control.inspect(handle.run_id)
        b = self.control.inspect(handle.run_id)
        self.assertEqual(a.trace_revision, b.trace_revision)
        self.assertNotEqual(a.trace_revision, "")


class SyncApiCompatibilityTests(LegacyRuntimeControlTestBase):
    """Compat proof required before M4 wiring: the adapter's RunResult and the
    existing POST /api/cases/{id}/agent-chat response carry the same contract
    for the same runtime path (plan A deliverable: 先补兼容测试，再启用接线)."""

    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_adapter_result_matches_agent_chat_response_contract(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            response = client.post("/api/cases/1/agent-chat", json={
                "question": QUESTION, "mode": "multi_agent",
                "use_llm": False, "use_remote_embeddings": False,
            })
        self.assertEqual(response.status_code, 200, response.text[:300])
        api = response.json()

        handle = self.control.start(make_request())
        self.assertEqual(handle.status, "completed")
        adapter = handle.result.to_legacy()

        # The route layers mode/user_name on top of the raw result; everything
        # else must be key-identical between the two paths.
        self.assertEqual(set(api.keys()) - {"mode", "user_name"}, set(adapter.keys()))
        self.assertEqual(api["agent_type"], adapter["agent_type"])
        self.assertEqual(api["agent_type"], "DAG Multi-Agent")
        self.assertEqual(api["route"], adapter["route"])
        self.assertEqual(api["runtime"], adapter["runtime"])
        self.assertEqual(api["runtime"], "native")
        for field_name in ("llm_used", "resumable", "memory_persisted", "citation_check", "resume_count"):
            self.assertEqual(api[field_name], adapter[field_name], field_name)
        # Parallel specialist steps land in completion order (nondeterministic
        # between siblings in BOTH paths); the node set and per-node fields are
        # the stable contract, not sibling ordering.
        self.assertEqual({step["node"] for step in api["steps"]},
                         {step["node"] for step in adapter["steps"]})
        api_steps = {step["node"]: step for step in api["steps"]}
        adapter_steps = {step["node"]: step for step in adapter["steps"]}
        self.assertEqual(set(api_steps["planner"].keys()), set(adapter_steps["planner"].keys()))
        self.assertEqual(len(api["citations"]), len(adapter["citations"]))
        self.assertEqual(set(api["citations"][0].keys()), set(adapter["citations"][0].keys()))
        self.assertEqual(set(api["retrieval_metrics"].keys()), set(adapter["retrieval_metrics"].keys()))
        # Two independent business runs (the API path and the adapter path),
        # each traceable through the same tables.
        self.assertNotEqual(api["run_id"], adapter["run_id"])
        trace = self.control.inspect(adapter["run_id"])
        self.assertEqual(trace.status, "completed")

    def test_route_audit_parity_is_documented_boundary(self):
        # The route writes one audit entry per successful chat; the M1 adapter
        # deliberately writes none (audit parity arrives with the M4 wiring).
        from fastapi.testclient import TestClient

        from app.main import app

        with closing(connect()) as conn:
            before = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='Agent 阅卷'").fetchone()[0]
        with TestClient(app) as client:
            response = client.post("/api/cases/1/agent-chat", json={
                "question": "梳理募集资金流向", "mode": "multi_agent",
                "use_llm": False, "use_remote_embeddings": False,
            })
        self.assertEqual(response.status_code, 200, response.text[:300])
        handle = self.control.start(make_request(question="梳理募集资金流向与用途"))
        self.assertEqual(handle.status, "completed")
        with closing(connect()) as conn:
            after = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='Agent 阅卷'").fetchone()[0]
        self.assertEqual(after, before + 1)  # route audited once; adapter added none


class SchemaMigrationTests(LegacyRuntimeControlTestBase):
    @classmethod
    def setUpClass(cls):
        cls.seed()

    def test_runtime_requests_table_exists_with_association_columns(self):
        with closing(connect()) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(runtime_requests)")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        from app.storage import db as app_db
        self.assertEqual(version, app_db.SCHEMA_VERSION)
        self.assertTrue({
            "request_id", "case_id", "runtime", "protocol_version", "run_id", "job_id",
            "created_at", "request_digest", "principal_name",
        } <= columns)

    def test_v14_database_gains_binding_columns_with_empty_defaults(self):
        import tempfile
        from unittest.mock import patch as mock_patch

        from app.storage import db
        with tempfile.TemporaryDirectory() as directory, db.db_scope(os.path.join(directory, "v14.db")):
            with mock_patch.object(db, "SCHEMA_VERSION", 14):
                db.init_db(seed=False)
            with db.transaction() as conn:
                case_id = conn.execute(
                    "INSERT INTO cases(title,description,created_at,updated_at) VALUES ('v14','x',?,?)",
                    (now(), now()),
                ).lastrowid
                conn.execute(
                    """INSERT INTO runtime_requests(request_id,case_id,runtime,protocol_version,created_at)
                       VALUES ('legacy-key',?,'native',1,?)""",
                    (case_id, now()),
                )
            db.init_db(seed=False)  # migrate v14 -> current schema; retain unbound identity
            with closing(db.connect()) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                row = conn.execute(
                    "SELECT request_digest, principal_name FROM runtime_requests WHERE request_id='legacy-key'"
                ).fetchone()
            self.assertEqual(row["request_digest"], "")
            self.assertEqual(row["principal_name"], "")


if __name__ == "__main__":
    unittest.main()
