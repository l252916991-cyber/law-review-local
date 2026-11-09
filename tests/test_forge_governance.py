"""E05 offline policy, accounting and actual framework model-protocol checks."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.integration.forge_readonly import ReadonlyCaseSession
from app.integration.forge_runtime import FrameworkReadonlyControl
from app.llm.forge_governance import BackendAdmissionError, BackendLedger, OfflineModelTest
from app.retrieval.rag import EmbeddingClient, RerankClient
from app.review.generation import call_local_llm
from app.storage.db import init_db
from tests.support import IsolatedDatabaseTestCase
from tests.test_forge_readonly import dump_source, request


class BackendGovernanceTests(unittest.TestCase):
    def test_legacy_keyword_call_signature_is_preserved_without_network(self):
        with patch("app.review.generation.assert_model_endpoint_allowed", side_effect=RuntimeError("body reached")):
            with self.assertRaisesRegex(RuntimeError, "body reached"):
                call_local_llm(question="question", route="route", contexts=[])
        ledger = BackendLedger(lambda: None)
        with ledger.activate(), self.assertRaises(BackendAdmissionError):
            call_local_llm(question="question", route="route", contexts=[])
        self.assertFalse(ledger.events[0]["executed"])

    def test_model_configuration_is_rejected_before_worker_start(self):
        for config in (OfflineModelTest(max_steps=1001), OfflineModelTest(max_context_tokens=0),
                       OfflineModelTest(max_steps=True), OfflineModelTest(scenario="unknown")):
            with self.subTest(config=config), self.assertRaises(ValueError):
                FrameworkReadonlyControl(model_test=config)

    def test_unbound_keyword_embedding_still_uses_backend_admission(self):
        ledger = BackendLedger(lambda: None)
        with ledger.activate():
            vectors, backend = EmbeddingClient.embed(self=EmbeddingClient(False), texts=["synthetic"])
        self.assertEqual(backend, "hashed-local")
        self.assertTrue(vectors)
        self.assertEqual(ledger.calls, 1)

    def test_local_hash_backend_is_counted_without_claiming_provider_tokens(self):
        ledger = BackendLedger(lambda: None)
        with ledger.activate():
            vectors, backend = EmbeddingClient(False).embed(["合成文本"])
        self.assertEqual(backend, "hashed-local")
        self.assertTrue(vectors)
        self.assertEqual(ledger.calls, 1)
        event = ledger.events[0]
        self.assertEqual(event["model"], "hashed-bigram-v1")
        self.assertTrue(event["executed"])
        self.assertFalse(event["usage_known"])
        self.assertIsNone(event["input_tokens"])
        self.assertNotIn("合成文本", str(event))

    def test_all_network_backend_kinds_are_denied_before_io(self):
        ledger = BackendLedger(lambda: None)
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden")):
            with ledger.activate():
                for operation in (lambda: EmbeddingClient(True).embed(["private"]),
                                  lambda: RerankClient().score("query", ["private"]),
                                  lambda: call_local_llm("question", "route", [])):
                    with self.assertRaises(BackendAdmissionError):
                        operation()
        self.assertEqual(ledger.calls, 0)
        self.assertEqual(len(ledger.events), 3)
        self.assertTrue(all(not event["executed"] for event in ledger.events))

    def test_call_and_input_budgets_deny_before_callback(self):
        for ledger in (BackendLedger(lambda: None, max_calls=1),
                       BackendLedger(lambda: None, max_input_chars=1)):
            called = []
            operation = lambda: called.append(True)
            client = SimpleNamespace(prefer_remote=False)
            if ledger.max_calls == 1:
                ledger.invoke("embedding", client, ("x",), {}, operation)
            before = len(called)
            with self.assertRaises(BackendAdmissionError):
                ledger.invoke("embedding", client, ("x",), {}, operation)
            self.assertEqual(len(called), before)

    def test_deadline_before_and_after_execution_is_truthful(self):
        clock = [0.0]
        ledger = BackendLedger(lambda: None, clock=lambda: clock[0])
        ledger.set_deadline(1.0)
        client = SimpleNamespace(prefer_remote=False)
        def slow():
            clock[0] = 2.0
        with self.assertRaises(BackendAdmissionError):
            ledger.invoke("embedding", client, (), {}, slow)
        self.assertTrue(ledger.events[-1]["executed"])
        self.assertEqual(ledger.events[-1]["outcome"], "failed")
        with self.assertRaises(BackendAdmissionError):
            ledger.invoke("embedding", client, (), {}, lambda: self.fail("expired callback"))
        self.assertFalse(ledger.events[-1]["executed"])

    def test_authorization_is_rechecked_after_backend_work(self):
        authorized = [True]
        def guard():
            if not authorized[0]:
                raise PermissionError("revoked")
        ledger = BackendLedger(guard)
        def revoke():
            authorized[0] = False
        with self.assertRaises(PermissionError):
            ledger.invoke("embedding", SimpleNamespace(prefer_remote=False), (), {}, revoke)
        self.assertEqual(ledger.events[-1]["outcome"], "failed")


@unittest.skipUnless(os.getenv("LAW_REVIEW_FORGE_PYTHON"), "separate framework interpreter required")
class GovernedModelProcessTests(IsolatedDatabaseTestCase):
    #: Snapshots of the SQLite file; refused on PostgreSQL (see app.storage.db.SQLITE_FILE_ONLY).
    sqlite_only = "framework snapshot reads the SQLite file"

    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def control(self, scenario="echo", **budget):
        return FrameworkReadonlyControl(model_test=OfflineModelTest(scenario=scenario, **budget))

    def test_scripted_generation_uses_framework_core_and_is_not_real_model_quality(self):
        before = dump_source()
        with patch("app.review.agents.call_local_llm", side_effect=AssertionError("real model forbidden")):
            handle = self.control().start(request())
        self.assertEqual(handle.status, "completed", handle.error)
        result = handle.result.to_legacy()
        self.assertEqual(result["model_backend"], "scripted-test")
        self.assertTrue(result["offline_model_test"])
        self.assertFalse(result["llm_used"])
        self.assertFalse(result["model_quality_assessed"])
        self.assertFalse(result["business_qualified"])
        self.assertEqual(result["framework_usage"]["steps"], 1)
        self.assertEqual(result["framework_usage"]["input_tokens"], 7)
        self.assertEqual(result["framework_usage"]["output_tokens"], 3)
        self.assertTrue(result["backend_calls"])
        self.assertEqual(before, dump_source())

    def test_bad_provider_protocol_empty_fabricated_quote_and_usage_cannot_succeed(self):
        before = dump_source()
        for scenario in ("provider_error", "protocol", "empty", "invalid_quote", "over_usage"):
            with self.subTest(scenario=scenario):
                handle = self.control(scenario).start(request())
                self.assertEqual(handle.status, "failed", handle)
                self.assertIsNotNone(handle.error)
                self.assertFalse(handle.result.to_legacy()["business_qualified"])
        self.assertEqual(before, dump_source())

    def test_prepared_input_budget_blocks_the_scripted_model(self):
        handle = self.control(max_context_tokens=1).start(request())
        self.assertEqual(handle.status, "failed", handle)
        usage = handle.result.to_legacy()["framework_usage"]
        self.assertEqual(usage["steps"], 0)

    def test_actual_usage_limit_keeps_consumed_test_tokens(self):
        handle = self.control(max_input_tokens=1).start(request())
        self.assertEqual(handle.status, "failed", handle)
        usage = handle.result.to_legacy()["framework_usage"]
        self.assertEqual(usage["steps"], 1)
        self.assertEqual(usage["input_tokens"], 7)

    def test_session_scope_installs_governance_and_does_not_leak_it(self):
        with ReadonlyCaseSession(request()) as session:
            session.call("planner"); session.call("memory_recall"); session.call("retrieve")
            self.assertTrue(session.backends.events)
            prior = len(session.backends.events)
        EmbeddingClient(False).embed(["outside snapshot"])
        self.assertEqual(len(session.backends.events), prior)
