"""Contract purity, validation and lossless-projection tests for the M1 freeze.

The runtime contract module must stay a pure boundary: standard library only,
no business imports, no framework imports (plan §5 M1 exit: "核心不导入具体
业务"). These tests enforce that structurally instead of by review alone.
"""
import ast
import os
import tempfile
import unittest
from pathlib import Path

TEST_DATA = tempfile.mkdtemp(prefix="lexvault-runtime-ports-")
os.environ.setdefault("LAW_REVIEW_DATA_DIR", TEST_DATA)

from app.integration import runtime_ports  # noqa: E402
from app.integration.runtime_adapter import LEGACY_CAPABILITIES, LegacyRuntimeControl  # noqa: E402
from app.integration.runtime_ports import (  # noqa: E402
    DEFAULT_TENANT_ID,
    EXECUTION_MODE_BACKGROUND,
    LANGGRAPH_RUNTIME,
    MAX_QUESTION_CHARS,
    NATIVE_RUNTIME,
    RUNTIME_PROTOCOL_VERSION,
    ActorIdentity,
    ContractValidationError,
    IdentityMismatchError,
    ModelConfig,
    ProtocolVersionError,
    RequestConflictError,
    RunBudget,
    RunEvent,
    RunRequest,
    RunResult,
    RunScope,
    RuntimeContractError,
    compute_request_digest,
    compute_trace_revision,
    new_request_id,
)

APP_ROOT = Path(__file__).resolve().parents[1]


def _imported_roots(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: record the package anchor
                roots.add("app")
            elif node.module:
                roots.add(node.module.split(".")[0])
    return roots


class ContractBoundaryTest(unittest.TestCase):
    def test_runtime_ports_imports_standard_library_only(self):
        roots = _imported_roots(APP_ROOT / "app" / "integration" / "runtime_ports.py")
        self.assertTrue(roots, "expected at least one import to analyze")
        allowed = {"__future__", "re", "uuid", "json", "hashlib", "dataclasses", "enum", "typing", "collections"}
        self.assertEqual(roots - allowed, set(), f"contract module leaked imports: {roots - allowed}")

    def test_contract_modules_never_import_forgeharness(self):
        # Structural check on import statements only: docstrings may name the
        # framework, code must not import it (M1 keeps the framework out of the
        # host process; M2 introduces the separate-process adapter instead).
        for name in ("runtime_ports.py", "runtime_adapter.py"):
            roots = _imported_roots(APP_ROOT / "app" / "integration" / name)
            self.assertNotIn("forgeharness", roots, f"{name} must not import the framework package")

    def test_adapter_import_surface_stays_host_side(self):
        # The adapter is host code and may import app modules plus fastapi for
        # the existing HTTPException-based authorization errors; nothing else.
        roots = _imported_roots(APP_ROOT / "app" / "integration" / "runtime_adapter.py")
        allowed = {"__future__", "collections", "json", "logging", "contextlib", "functools", "typing", "fastapi", "app"}
        self.assertEqual(roots - allowed, set(), f"adapter leaked imports: {roots - allowed}")
        # And the dependency direction is one-way: ports must not import adapter.
        self.assertNotIn("app", _imported_roots(APP_ROOT / "app" / "integration" / "runtime_ports.py"))


class RunRequestValidationTest(unittest.TestCase):
    def request(self, **overrides):
        base = dict(
            request_id=new_request_id(),
            scope=RunScope(case_id=1),
            actor=ActorIdentity("本机律师"),
            question="固定回报审批是否存在矛盾？",
        )
        base.update(overrides)
        return RunRequest(**base)

    def test_generated_request_ids_are_protocol_conformant(self):
        for _ in range(20):
            self.assertRegex(new_request_id(), runtime_ports.REQUEST_ID_PATTERN)

    def test_valid_request_passes(self):
        self.request().validate()

    def test_protocol_version_mismatch_is_rejected(self):
        with self.assertRaises(ProtocolVersionError):
            self.request(protocol_version=RUNTIME_PROTOCOL_VERSION + 1).validate()
        with self.assertRaises(ProtocolVersionError):
            self.request(protocol_version=0).validate()

    def test_request_id_grammar_is_enforced(self):
        for bad in ("", "_lead", "含中文", "a" * 129, " spaced"):
            with self.assertRaises(ContractValidationError, msg=bad):
                self.request(request_id=bad).validate()

    def test_question_bounds_match_review_job_contract(self):
        with self.assertRaises(ContractValidationError):
            self.request(question="   ").validate()
        with self.assertRaises(ContractValidationError):
            self.request(question="长" * (MAX_QUESTION_CHARS + 1)).validate()
        self.request(question="长" * MAX_QUESTION_CHARS).validate()

    def test_scope_and_mode_bounds(self):
        with self.assertRaises(ContractValidationError):
            self.request(scope=RunScope(case_id=0)).validate()
        with self.assertRaises(ContractValidationError):
            self.request(scope=RunScope(case_id=1, tenant_id="")).validate()
        with self.assertRaises(ContractValidationError):
            self.request(execution_mode="telepathy").validate()
        self.request(execution_mode=EXECUTION_MODE_BACKGROUND).validate()


class RunResultRoundTripTest(unittest.TestCase):
    NATIVE_PAYLOAD = {
        "run_id": 7,
        "answer": "答案 [资料1]，请律师复核。",
        "route": "多文档对比",
        "agent_type": "DAG Multi-Agent",
        "plan": [{"name": "facts", "role": "事实 Agent"}],
        "steps": [{"id": 1, "node": "planner", "status": "completed"}],
        "tools_used": ["BM25", "RRF"],
        "citations": [{"index": 1, "document_id": 2, "page": 3, "quote": "原文"}],
        "retrieval_metrics": {"mode": "FTS5/BM25 + Legal Lexical + Vector + RRF"},
        "memory_hits": [],
        "llm_used": False,
        "llm_model": "Qwythos-9B-v2-4bit-mlx",
        "fallback_reason": "llm_disabled",
        "validation": {"non_empty": True},
        "citation_check": "通过",
        "failure_diagnostic": None,
        "memory_persisted": True,
        "total_ms": 1234,
        "runtime": "native",
        "checkpoint_thread_id": "",
        "resumable": False,
        "resume_count": 0,
    }
    LANGGRAPH_PAYLOAD = {
        **NATIVE_PAYLOAD,
        "run_id": 9,
        "agent_type": "LangGraph Multi-Agent",
        "runtime": "langgraph",
        "mode": "langgraph",
        "user_name": "测试律师",
        "checkpoint_thread_id": "agent-run-9",
        "checkpoint_size_bytes": 4096,
        "conversation_id": 3,
    }

    def test_native_payload_round_trips_exactly(self):
        result = RunResult.from_legacy(self.NATIVE_PAYLOAD)
        self.assertEqual(result.to_legacy(), self.NATIVE_PAYLOAD)
        self.assertEqual(result.runtime, NATIVE_RUNTIME)
        self.assertFalse(result.resumable)

    def test_langgraph_extras_survive_round_trip(self):
        result = RunResult.from_legacy(self.LANGGRAPH_PAYLOAD)
        self.assertEqual(result.to_legacy(), self.LANGGRAPH_PAYLOAD)
        self.assertEqual(result.runtime, LANGGRAPH_RUNTIME)
        self.assertEqual(result.extra["agent_type"], "LangGraph Multi-Agent")
        self.assertEqual(result.extra["checkpoint_size_bytes"], 4096)
        self.assertEqual(result.extra["conversation_id"], 3)

    def test_future_legacy_keys_are_preserved(self):
        payload = {**self.NATIVE_PAYLOAD, "brand_new_field": {"x": 1}}
        self.assertEqual(RunResult.from_legacy(payload).to_legacy(), payload)

    def test_missing_optional_keys_do_not_crash(self):
        result = RunResult.from_legacy({"run_id": 1, "answer": "a"})
        self.assertEqual(result.run_id, 1)
        self.assertEqual(result.citations, [])
        self.assertEqual(result.to_legacy()["runtime"], "")


class RunEventProjectionTest(unittest.TestCase):
    def test_event_maps_step_fields_and_diagnostic(self):
        step = {
            "node_name": "facts", "agent_role": "事实 Agent", "status": "failed",
            "started_at": "2026-10-21T10:00:00+08:00", "finished_at": "2026-10-21T10:00:01+08:00",
            "latency_ms": 900, "output": {"diagnostic": {"code": "facts_failed"}},
        }
        event = RunEvent.from_legacy_step(42, 3, step)
        self.assertEqual((event.run_id, event.seq, event.node_id), (42, 3, "facts"))
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.latency_ms, 900)
        self.assertEqual(event.diagnostic, {"code": "facts_failed"})
        self.assertIsNone(event.usage)  # legacy runtime attributes no per-node usage
        self.assertEqual(event.event_version, runtime_ports.RUN_EVENT_VERSION)

    def test_event_tolerates_completed_steps_without_diagnostic(self):
        event = RunEvent.from_legacy_step(1, 1, {"node_name": "critic", "status": "completed", "output": {"summary": "ok"}})
        self.assertIsNone(event.diagnostic)


class CapabilityDeclarationTest(unittest.TestCase):
    def test_legacy_capabilities_are_declared_not_inferred(self):
        caps = LegacyRuntimeControl().capabilities()
        self.assertIs(caps, LEGACY_CAPABILITIES)
        self.assertEqual(caps.protocol_version, RUNTIME_PROTOCOL_VERSION)
        self.assertEqual(caps.runtimes, frozenset({NATIVE_RUNTIME, LANGGRAPH_RUNTIME}))
        self.assertEqual(caps.start_modes, frozenset({"sync"}))
        self.assertEqual(caps.resumable_runtimes, frozenset({LANGGRAPH_RUNTIME}))
        self.assertFalse(caps.background_start)
        self.assertFalse(caps.budget_enforcement)
        self.assertTrue(caps.event_projection)
        self.assertTrue(caps.idempotent_start)
        self.assertTrue(caps.persistent_request_mapping)
        self.assertEqual(caps.supported_tenants, frozenset({DEFAULT_TENANT_ID}))

    def test_model_config_defaults_match_host_defaults(self):
        config = ModelConfig()
        self.assertTrue(config.use_llm)
        self.assertIsNone(config.model)
        self.assertIsNone(config.timeout_seconds)

    def test_contract_version_values_are_frozen(self):
        # Any change here is a contract decision, not a refactor: bump
        # deliberately and register it in docs/history/migration/m1-contracts.md §8.
        self.assertEqual(runtime_ports.RUNTIME_PROTOCOL_VERSION, 2)
        self.assertEqual(runtime_ports.RUN_EVENT_VERSION, 2)
        self.assertEqual(runtime_ports.RUN_RESULT_VERSION, 1)
        self.assertEqual(runtime_ports.REQUEST_DIGEST_SCHEME, "lexvault-request-binding/1")
        self.assertEqual(runtime_ports.TRACE_REVISION_SCHEME, "lexvault-trace-revision/1")

    def test_new_errors_join_contract_hierarchy(self):
        for error in (IdentityMismatchError, RequestConflictError):
            self.assertTrue(issubclass(error, RuntimeContractError))


class RequestDigestTest(unittest.TestCase):
    def request(self, **overrides):
        base = dict(
            request_id="fixed-key",
            scope=RunScope(case_id=1),
            actor=ActorIdentity("本机律师", admin=True),
            question="固定回报审批是否存在矛盾？",
        )
        base.update(overrides)
        return RunRequest(**base)

    def digest(self, request, **identity):
        identity.setdefault("principal_name", "本机律师")
        identity.setdefault("effective_author", "本机律师")
        return compute_request_digest(request, **identity)

    def test_digest_is_deterministic_and_question_whitespace_normalized(self):
        a = self.digest(self.request())
        b = self.digest(self.request())
        c = self.digest(self.request(question="  固定回报审批是否存在矛盾？  "))
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertRegex(a, r"^[a-f0-9]{64}$")

    def test_every_execution_relevant_field_discriminates(self):
        base = self.digest(self.request())
        variants = [
            self.request(scope=RunScope(case_id=2)),
            self.request(scope=RunScope(case_id=1, tenant_id="other")),
            self.request(runtime="langgraph"),
            self.request(question="另一个问题"),
            self.request(use_remote_embeddings=False),
            self.request(persist_memory=False),
            self.request(memory_snapshot=({"content": "记忆"},)),
            self.request(model=ModelConfig(use_llm=False)),
            self.request(model=ModelConfig(model="other-model")),
            self.request(model=ModelConfig(timeout_seconds=30)),
            self.request(budget=RunBudget(max_steps=10)),
            self.request(execution_mode=EXECUTION_MODE_BACKGROUND),
        ]
        for variant in variants:
            self.assertNotEqual(base, self.digest(variant), f"digest missed a field: {variant}")
        self.assertNotEqual(base, self.digest(self.request(), principal_name="另一位律师"))
        self.assertNotEqual(base, self.digest(self.request(), effective_author="另一位作者"))

    def test_descriptive_versions_do_not_discriminate(self):
        base = self.digest(self.request())
        self.assertEqual(base, self.digest(self.request(app_version="9.9.9")))
        self.assertEqual(base, self.digest(self.request(framework_version="9.9.9")))
        self.assertEqual(base, self.digest(self.request(data_snapshot_version="9.9.9")))

    def test_memory_snapshot_container_shape_is_canonical(self):
        as_tuple = self.digest(self.request(memory_snapshot=({"a": 1}, {"b": 2})))
        as_list = compute_request_digest(
            self.request(memory_snapshot=[{"a": 1}, {"b": 2}]),
            principal_name="本机律师", effective_author="本机律师",
        )
        self.assertEqual(as_tuple, as_list)


class TraceRevisionTest(unittest.TestCase):
    RUN = {"status": "failed", "route": "语义检索", "runtime": "langgraph", "resume_count": 0,
           "total_ms": 10, "finished_at": "t1", "final_answer": "", "checkpoint_thread_id": "agent-run-1",
           "retrieval_mode": "hybrid_rrf", "citations": []}
    STEP = {"id": 1, "node_name": "memory", "agent_role": "记忆 Agent", "status": "failed",
            "latency_ms": 5, "started_at": "t0", "finished_at": "t1",
            "input": {}, "output": {"summary": "x_failed"}}

    def test_revision_is_deterministic(self):
        a = compute_trace_revision(self.RUN, [self.STEP])
        b = compute_trace_revision(dict(self.RUN), [dict(self.STEP)])
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[a-f0-9]{64}$")

    def test_in_place_step_update_changes_revision(self):
        before = compute_trace_revision(self.RUN, [self.STEP])
        updated = {**self.STEP, "status": "completed", "output": {"summary": "已写入记忆"}}
        after = compute_trace_revision({**self.RUN, "status": "completed", "resume_count": 1}, [updated])
        self.assertNotEqual(before, after)

    def test_run_field_and_ordering_changes_are_detected(self):
        base = compute_trace_revision(self.RUN, [self.STEP, {**self.STEP, "id": 2, "node_name": "critic"}])
        self.assertNotEqual(base, compute_trace_revision({**self.RUN, "total_ms": 11}, [self.STEP, {**self.STEP, "id": 2, "node_name": "critic"}]))
        self.assertNotEqual(base, compute_trace_revision(self.RUN, [{**self.STEP, "id": 2, "node_name": "critic"}, self.STEP]))
        self.assertNotEqual(base, compute_trace_revision({**self.RUN, "citations": [{"index": 1}]}, [self.STEP, {**self.STEP, "id": 2, "node_name": "critic"}]))


if __name__ == "__main__":
    unittest.main()
