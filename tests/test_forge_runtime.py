"""E04 acceptance tests: the framework read-only RuntimeControl surface.

Covers honest capability declaration, explicit rejection of unsupported
capabilities (never a silent degradation), and — when
``LAW_REVIEW_FORGE_PYTHON`` points at the separately installed Python 3.12
framework environment — one real governed run through ``start()`` asserting
the governance facts, the pass/fail honesty and source-database immutability.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

TEST_DATA = tempfile.mkdtemp(prefix="lexvault-forge-runtime-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA

from app.core.security import Principal, _principal  # noqa: E402
from app.integration.forge_runtime import (  # noqa: E402
    FRAMEWORK_READONLY_CAPABILITIES,
    FRAMEWORK_READONLY_RUNTIME,
    FrameworkReadonlyControl,
    framework_readonly_control,
    resolve_framework_python,
)
from app.integration.runtime_adapter import current_actor_identity  # noqa: E402
from app.integration.runtime_ports import (  # noqa: E402
    EXECUTION_MODE_BACKGROUND,
    AccessDeniedError,
    ContractValidationError,
    ModelConfig,
    ResumeNotSupportedError,
    RunBudget,
    RunRequest,
    RunScope,
    UnsupportedCapabilityError,
    UnsupportedExecutionModeError,
    new_request_id,
)
from app.storage.db import connect, init_db, now, transaction  # noqa: E402
from tests.support import IsolatedDatabaseTestCase  # noqa: E402

FRAMEWORK_PYTHON = os.getenv("LAW_REVIEW_FORGE_PYTHON", "")


def request(**changes) -> RunRequest:
    base = RunRequest(
        request_id=new_request_id(),
        scope=RunScope(1),
        actor=current_actor_identity(),
        question="本案审批陈述是否矛盾并存在证据疏漏？",
        runtime=FRAMEWORK_READONLY_RUNTIME,
        model=ModelConfig(use_llm=False),
        use_remote_embeddings=False,
        persist_memory=False,
    )
    return replace(base, **changes)


def dump_source() -> tuple:
    with closing(connect()) as conn:
        return tuple(conn.iterdump())


class CapabilityDeclarationTests(unittest.TestCase):
    def test_capabilities_are_declared_honestly(self):
        control = framework_readonly_control()
        self.assertIsInstance(control, FrameworkReadonlyControl)
        capabilities = control.capabilities()
        self.assertIs(capabilities, FRAMEWORK_READONLY_CAPABILITIES)
        self.assertEqual(capabilities.runtimes, frozenset({FRAMEWORK_READONLY_RUNTIME}))
        self.assertEqual(capabilities.start_modes, frozenset({"sync"}))
        self.assertEqual(capabilities.resumable_runtimes, frozenset())
        self.assertFalse(capabilities.background_start)
        self.assertFalse(capabilities.budget_enforcement)
        self.assertFalse(capabilities.event_projection)
        self.assertFalse(capabilities.idempotent_start)
        self.assertFalse(capabilities.persistent_request_mapping)

    def test_invalid_timeout_is_a_contract_error(self):
        for bad in (0, -1, 601):
            with self.subTest(timeout=bad), self.assertRaises(ContractValidationError):
                FrameworkReadonlyControl(timeout_seconds=bad)


class StartValidationTests(IsolatedDatabaseTestCase):
    #: Snapshots of the SQLite file; refused on PostgreSQL (see app.storage.db.SQLITE_FILE_ONLY).
    sqlite_only = "framework snapshot reads the SQLite file"
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def control(self) -> FrameworkReadonlyControl:
        return FrameworkReadonlyControl(framework_python="/nonexistent/python")

    def test_other_runtimes_and_modes_are_rejected(self):
        with self.assertRaises(ContractValidationError):
            self.control().start(request(runtime="native"))
        with self.assertRaises(UnsupportedExecutionModeError):
            self.control().start(request(execution_mode=EXECUTION_MODE_BACKGROUND))

    def test_unsupported_capabilities_are_rejected_explicitly(self):
        variants = {
            "budget": dict(budget=RunBudget(max_steps=1)),
            "model override": dict(model=ModelConfig(use_llm=False, model="custom")),
            "llm enabled": dict(model=ModelConfig(use_llm=True)),
            "remote embeddings": dict(use_remote_embeddings=True),
            "memory persistence": dict(persist_memory=True),
        }
        for label, changes in variants.items():
            with self.subTest(capability=label), self.assertRaises(UnsupportedCapabilityError):
                self.control().start(request(**changes))

    def test_missing_framework_interpreter_is_rejected(self):
        with patch.dict(os.environ, {"LAW_REVIEW_FORGE_PYTHON": ""}):
            with self.assertRaises(UnsupportedCapabilityError):
                resolve_framework_python()
            with self.assertRaises(UnsupportedCapabilityError):
                FrameworkReadonlyControl().start(request())
        with self.assertRaises(UnsupportedCapabilityError):
            resolve_framework_python("/nonexistent/python")

    def test_unsupported_control_methods_are_rejected(self):
        control = self.control()
        with self.assertRaises(UnsupportedCapabilityError):
            control.inspect(1)
        with self.assertRaises(ResumeNotSupportedError):
            control.resume(1)
        with self.assertRaises(UnsupportedCapabilityError):
            control.status("job")

    def test_unauthorized_case_is_denied_before_launching_a_process(self):
        before = dump_source()
        marker = _principal.set(
            Principal("受限律师", False, (999,), permissions=frozenset({"view"}))
        )
        try:
            with self.assertRaises(AccessDeniedError):
                self.control().start(request())
        finally:
            _principal.reset(marker)
        self.assertEqual(before, dump_source())


@unittest.skipUnless(FRAMEWORK_PYTHON, "set LAW_REVIEW_FORGE_PYTHON to the separately installed Python 3.12 sidecar")
class FrameworkProcessControlTests(IsolatedDatabaseTestCase):
    #: Snapshots of the SQLite file; refused on PostgreSQL (see app.storage.db.SQLITE_FILE_ONLY).
    sqlite_only = "framework snapshot reads the SQLite file"
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def control(self) -> FrameworkReadonlyControl:
        return FrameworkReadonlyControl(framework_python=FRAMEWORK_PYTHON, compare_native=True)

    def test_start_returns_governed_result_and_leaves_the_source_unchanged(self):
        before = dump_source()
        handle = self.control().start(request())
        self.assertEqual(handle.status, "completed", handle.error)
        self.assertTrue(handle.started_new)
        legacy = handle.result.to_legacy()
        self.assertEqual(legacy["runtime"], FRAMEWORK_READONLY_RUNTIME)
        self.assertTrue(legacy["strict_answer"])
        self.assertTrue(legacy["citations"])
        self.assertTrue(legacy["framework_trace_valid"])
        self.assertGreater(legacy["framework_trace_events"], 0)
        self.assertEqual(legacy["framework_version"], "0.8.3")
        self.assertEqual(legacy["wheel_sha256"],
                         "1d5880ed2ba1cabcb20b0970a55727bcca7b68f9b4beace70ce7600d58ba7555")
        self.assertEqual(legacy["source_business_writes"], 0)
        self.assertFalse(legacy["business_qualified"])
        self.assertFalse(legacy["model_quality_assessed"])
        self.assertTrue(legacy["port_access_log"])
        comparison = legacy["comparison"]
        self.assertEqual(comparison["mode"], "shared_retrieval_and_memory_snapshot")
        self.assertTrue(comparison["passed"])
        self.assertFalse(comparison["model_quality_assessed"])
        self.assertEqual(before, dump_source())

    def test_insufficient_evidence_is_reported_as_failure_not_success(self):
        with transaction() as conn:
            empty_case = conn.execute(
                "INSERT INTO cases(title,description,created_at,updated_at) VALUES ('空案','无材料',?,?)",
                (now(), now()),
            ).lastrowid
        before = dump_source()
        handle = self.control().start(request(scope=RunScope(int(empty_case))))
        self.assertEqual(handle.status, "failed")
        self.assertIsNotNone(handle.error)
        self.assertEqual(handle.error.code, "framework_readonly_failed")
        self.assertFalse(handle.error.resumable)
        self.assertIsNotNone(handle.result)  # the report still carries the governed result facts
        self.assertEqual(handle.result.to_legacy()["citation_check"], "")
        self.assertEqual(before, dump_source())


if __name__ == "__main__":
    unittest.main()
