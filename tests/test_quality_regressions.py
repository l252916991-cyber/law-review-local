"""Isolated regressions for grounded retrieval and both review runtimes.

No test here calls a real model or opens the application's production database.
"""

import io
import json
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agents import (
    CriticAgent, EvidenceAgent, FactAgent, GapDetectionAgent, LawReviewCoordinator,
    get_run_trace,
)
from app.db import connect, db_scope, init_db, now, transaction
from app.evaluation import evaluate_case, platform_metrics
from app.langgraph_agents import LangGraphCoordinator, LangGraphRunError
from app.rag import (
    EmbeddingClient, HybridRetriever, embedding_identity, hashed_embedding,
    recall_memories, remember,
)
from app.runtime_comparison import compare_runtime_results
from app.services import call_local_llm, chat, read_json_with_deadline


class IsolatedQualityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lexvault-quality-")
        self.addCleanup(self.temp.cleanup)
        self.scope = db_scope(Path(self.temp.name) / "quality.sqlite")
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        init_db(seed=False)
        with transaction() as conn:
            self.case_id = conn.execute(
                "INSERT INTO cases(title, created_at, updated_at) VALUES (?, ?, ?)",
                ("隔离案件", now(), now()),
            ).lastrowid
            self.document_id = conn.execute(
                "INSERT INTO documents(case_id, name, doc_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (self.case_id, "流水.txt", "银行流水", now(), now()),
            ).lastrowid
            self.page_id = conn.execute(
                "INSERT INTO pages(document_id, page_no, text) VALUES (?, ?, ?)",
                (self.document_id, 1, "固定回报已经确认。张某的投资款转入公司账户。"),
            ).lastrowid
        self.context = {
            "document_id": self.document_id, "name": "流水.txt", "page_no": 1,
            "doc_type": "银行流水", "text": "固定回报已经确认。", "quote": "固定回报已经确认。",
        }

    def retriever(self):
        return HybridRetriever(self.case_id, prefer_remote_embeddings=False)

    def graph(self, **kwargs):
        return LangGraphCoordinator(
            self.case_id, False, checkpoint_path=Path(self.temp.name) / "checkpoint.sqlite", **kwargs
        )

    def create_run(self):
        with transaction() as conn:
            return conn.execute(
                "INSERT INTO agent_runs(case_id, question, route, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (self.case_id, "固定回报", "语义检索", "completed", now()),
            ).lastrowid


class VectorCompatibilityTest(IsolatedQualityTest):
    def test_same_dimension_different_model_is_reembedded(self):
        retriever = self.retriever()
        with patch.object(retriever.embedding_client, "embed", side_effect=lambda texts: ([[1.0, 0.0] for _ in texts], "omlx")):
            retriever.embedding_client.model = "model-a"
            retriever.ensure_vector_index()
            retriever.embedding_client.model = "model-b"
            rebuilt = retriever.ensure_vector_index()
        self.assertEqual(rebuilt["embedded"], 1)
        self.assertEqual(rebuilt["model"], embedding_identity("model-b", "omlx"))

    def test_dimensions_backend_and_corrupted_cache_are_not_reused(self):
        retriever = self.retriever()
        retriever.ensure_vector_index()
        for field, value in (("dimensions", 2), ("backend", "omlx"), ("model", "old-preprocessing"), ("vector_json", "not-json")):
            with self.subTest(field=field):
                with transaction() as conn:
                    conn.execute(f"UPDATE embedding_cache SET {field}=?", (value,))
                self.assertEqual(retriever.ensure_vector_index()["embedded"], 1)

    def test_remote_failure_mid_index_preserves_existing_space(self):
        retriever = self.retriever()
        retriever.ensure_vector_index()
        with closing(connect()) as conn:
            before = [tuple(row) for row in conn.execute("SELECT * FROM embedding_cache")]
        retriever.embedding_client.prefer_remote = True
        with patch.object(retriever.embedding_client, "embed", side_effect=[
            ([[1.0, 0.0]], "omlx"), ([hashed_embedding("page")], "hashed-local"),
        ]), self.assertRaisesRegex(RuntimeError, "embedding_backend_changed_during_index"):
            retriever.vector_search("固定回报")
        with closing(connect()) as conn:
            self.assertEqual(before, [tuple(row) for row in conn.execute("SELECT * FROM embedding_cache")])

    def test_backend_recovery_replaces_hashed_cache(self):
        retriever = self.retriever()
        retriever.ensure_vector_index()
        retriever.embedding_client.prefer_remote = True
        with patch.object(retriever.embedding_client, "embed", side_effect=lambda texts: ([[1.0, 0.0] for _ in texts], "omlx")):
            results = retriever.vector_search("固定回报")
        self.assertTrue(results)
        self.assertFalse(retriever.vector_diagnostics["degraded"])
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT backend FROM embedding_cache").fetchone()[0], "omlx")

    def test_concurrent_model_change_is_skipped_not_compared(self):
        retriever = self.retriever()
        retriever.ensure_vector_index()
        original = retriever.ensure_vector_index

        def changed_after_index(**kwargs):
            stats = original(**kwargs)
            with transaction() as conn:
                conn.execute("UPDATE embedding_cache SET model='different-same-dimensional-model'")
            return stats

        with patch.object(retriever, "ensure_vector_index", side_effect=changed_after_index):
            self.assertEqual(retriever.vector_search("固定回报"), [])
        self.assertEqual(retriever.vector_diagnostics["incompatible_vectors_skipped"], 1)

    def test_retrieval_never_rebuilds_global_fts_and_reports_degradation(self):
        with patch("app.db.sync_fts_index", side_effect=AssertionError("no global rebuild")):
            results, metrics = self.retriever().retrieve("固定回报")
        self.assertTrue(results)
        self.assertTrue(metrics["degraded"])
        self.assertEqual(metrics["embedding"]["backend"], "hashed-local")
        self.assertEqual(metrics["source_count"], len(results))

    def test_fts_fallback_is_visible_and_logs_no_case_text(self):
        with transaction() as conn:
            conn.execute("DROP TABLE pages_fts")
        with self.assertLogs("app.rag", level="WARNING") as logs:
            results, metrics = self.retriever().retrieve("固定回报")
        self.assertTrue(results)
        self.assertFalse(metrics["keyword"]["fts_available"])
        self.assertNotIn("固定回报", " ".join(logs.output))

    def test_invalid_embedding_payload_fails_closed(self):
        client = EmbeddingClient(True)
        invalid = io.BytesIO(json.dumps({"data": [{"index": 0, "embedding": [float("nan"), 1.0]}]}).encode())
        with patch("app.rag.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = invalid
            with self.assertRaisesRegex(RuntimeError, "embedding_model_unavailable:ValueError"):
                client.embed(["private text"])
        self.assertEqual(client.last_failure, "ValueError")


class CriticAndMemoryTest(IsolatedQualityTest):
    def critic(self, answer, contexts=None):
        contexts = [self.context] if contexts is None else contexts
        specialists = {"facts": FactAgent().run("问题", contexts)}
        with patch("app.agents.call_local_llm", return_value=answer):
            return CriticAgent().run("问题", "语义检索", contexts, specialists, True)

    def test_critic_rejects_empty_invalid_missing_and_malformed_citations(self):
        for answer in ("", "秘密正文[资料99]，请律师复核。", "无引用，需律师复核。", "秘密正文[资料1,2]，需律师复核。", "结论[资料1]。", "结论[资料1]，无需律师复核。", "结论[资料1]和【资料99】，请律师复核。"):
            with self.subTest(answer=answer):
                result = self.critic(answer)
                self.assertFalse(result["llm_used"])
                self.assertFalse(result["memory_eligible"])
                self.assertTrue(result["rejected_llm_validation"]["issues"])
                self.assertNotIn("秘密正文", result["answer"])

    def test_structural_pass_never_claims_semantic_or_arithmetic_verification(self):
        result = self.critic("结论依据[资料 1]，需律师复核。")
        self.assertTrue(result["llm_used"])
        self.assertTrue(result["validation"]["valid"])
        self.assertFalse(result["validation"]["semantic_entailment_checked"])
        self.assertFalse(result["validation"]["amount_calculations_checked"])

    def test_source_quote_and_page_must_exist(self):
        for change in ({"quote": ""}, {"page_no": 0}, {"document_id": -1}):
            with self.subTest(change=change):
                result = self.critic("依据[资料1]，需律师复核。", [{**self.context, **change}])
                self.assertFalse(result["memory_eligible"])
                self.assertIn("sources_valid", result["validation"]["issues"])

    def test_failure_details_do_not_leak_into_fallback_trace_or_memory(self):
        with patch("app.agents.call_local_llm", side_effect=RuntimeError("SECRET_TOKEN sensitive case")):
            result = LawReviewCoordinator(self.case_id, False).process_query("固定回报", use_llm=True)
        self.assertFalse(result["memory_persisted"])
        self.assertEqual(result["failure_diagnostic"]["error_type"], "RuntimeError")
        self.assertNotIn("SECRET_TOKEN", json.dumps(get_run_trace(result["run_id"])))
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)

    def test_empty_context_is_not_a_passed_or_memorable_answer(self):
        result = self.critic("无材料，请律师复核。", [])
        self.assertFalse(result["memory_eligible"])
        self.assertEqual(result["citation_check"], "failed")

    def test_memory_requires_validation_is_idempotent_and_labeled_draft(self):
        run_id = self.create_run()
        with self.assertRaises(ValueError):
            remember(self.case_id, "律师", "固定回报", run_id, prefer_remote_embeddings=False)
        first = remember(self.case_id, "律师", "固定回报", run_id, prefer_remote_embeddings=False, validated=True)
        second = remember(self.case_id, "律师", "固定回报", run_id, prefer_remote_embeddings=False, validated=True)
        self.assertEqual(first, second)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT kind FROM memories").fetchone()[0], "agent_draft_validated")

    def test_memory_incompatible_vectors_are_reembedded_not_silently_compared(self):
        run_id = self.create_run()
        remember(self.case_id, "律师", "固定回报", run_id, prefer_remote_embeddings=False, validated=True)
        with transaction() as conn:
            conn.execute("UPDATE memories SET embedding_model='old-model', vector_json='[0,1]'")
        with patch.object(EmbeddingClient, "embed", side_effect=lambda texts: ([[1.0, 0.0] for _ in texts], "omlx")) as embed:
            memories = recall_memories(self.case_id, "固定回报", prefer_remote_embeddings=True)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["similarity"], 1.0)
        self.assertEqual(embed.call_count, 2)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT vector_json FROM memories").fetchone()[0], "[0,1]")

    def test_legacy_unvalidated_memory_is_not_recalled(self):
        run_id = self.create_run()
        remember(self.case_id, "律师", "固定回报", run_id, prefer_remote_embeddings=False, validated=True)
        with transaction() as conn:
            conn.execute("UPDATE memories SET kind='agent_conclusion'")
        self.assertEqual(recall_memories(self.case_id, "固定回报", prefer_remote_embeddings=False), [])

    def test_mismatched_memories_are_reembedded_in_bounded_batches(self):
        for suffix in ("甲", "乙", "丙"):
            run_id = self.create_run()
            remember(self.case_id, "律师", f"固定回报{suffix}", run_id,
                     prefer_remote_embeddings=False, validated=True)
        with transaction() as conn:
            conn.execute("UPDATE memories SET embedding_model='old-space', vector_json='[0,1]'")
        with patch.object(EmbeddingClient, "embed",
                          side_effect=lambda texts: ([[1.0, 0.0] for _ in texts], "omlx")) as embed:
            memories = recall_memories(self.case_id, "固定回报", limit=10, prefer_remote_embeddings=True)
        self.assertEqual(len(memories), 3)
        self.assertEqual(embed.call_count, 2)  # one query + one batch, not one call per memory

    def test_memory_from_failed_run_is_not_recalled(self):
        run_id = self.create_run()
        remember(self.case_id, "律师", "固定回报", run_id, prefer_remote_embeddings=False, validated=True)
        with transaction() as conn:
            conn.execute("UPDATE agent_runs SET status='failed' WHERE id=?", (run_id,))
        self.assertEqual(recall_memories(self.case_id, "固定回报", prefer_remote_embeddings=False), [])

    def test_memory_source_cannot_be_attached_to_another_case(self):
        run_id = self.create_run()
        with transaction() as conn:
            other_case = conn.execute(
                "INSERT INTO cases(title, created_at, updated_at) VALUES (?, ?, ?)",
                ("其他案件", now(), now()),
            ).lastrowid
        with self.assertRaises(ValueError):
            remember(other_case, "律师", "固定回报", run_id, prefer_remote_embeddings=False, validated=True)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)


class RuntimeQualityTest(IsolatedQualityTest):
    def test_native_and_graph_dependencies_parallelism_and_parity(self):
        original_fact, original_evidence = FactAgent.run, EvidenceAgent.run
        original_gap = GapDetectionAgent.run
        for runtime in (LawReviewCoordinator(self.case_id, False), self.graph()):
            done = set()
            barrier = threading.Barrier(2)

            def facts(agent, question, contexts):
                barrier.wait(timeout=3)
                output = original_fact(agent, question, contexts)
                done.add("facts")
                return output

            def evidence(agent, question, contexts):
                barrier.wait(timeout=3)
                output = original_evidence(agent, question, contexts)
                done.add("evidence")
                return output

            def gap(agent, question, contexts):
                self.assertEqual(done, {"facts", "evidence"})
                return original_gap(agent, question, contexts)

            with patch.object(FactAgent, "run", facts), patch.object(EvidenceAgent, "run", evidence), patch.object(GapDetectionAgent, "run", gap):
                result = runtime.process_query("检查固定回报证据疏漏", use_llm=False, persist_memory=False, memory_snapshot=[])
            if isinstance(runtime, LawReviewCoordinator):
                native = result
            else:
                comparison = compare_runtime_results(native, result)
                self.assertTrue(comparison["equivalent"], comparison)

    def test_native_node_failure_has_safe_structured_trace(self):
        with patch.object(FactAgent, "run", side_effect=RuntimeError("SECRET_CASE_TEXT")):
            with self.assertRaises(RuntimeError):
                LawReviewCoordinator(self.case_id, False).process_query("固定回报", use_llm=False)
        with closing(connect()) as conn:
            run_id = conn.execute("SELECT MAX(id) FROM agent_runs").fetchone()[0]
        trace = get_run_trace(run_id)
        self.assertEqual(trace["status"], "failed")
        failed = [step for step in trace["steps"] if step["status"] == "failed"]
        self.assertEqual(failed[0]["node_name"], "facts")
        self.assertEqual(failed[0]["output"]["diagnostic"]["error_type"], "RuntimeError")
        self.assertNotIn("SECRET_CASE_TEXT", json.dumps(trace))

    def test_graph_resume_preserves_validated_critic_without_repeat_llm(self):
        def inject(node, state):
            if node == "memory":
                raise RuntimeError("SECRET memory failure")

        with patch("app.agents.call_local_llm", return_value="依据[资料1]，请律师复核。") as llm:
            with self.assertRaises(LangGraphRunError) as caught:
                self.graph(failure_injector=inject).process_query("固定回报", use_llm=True)
            result = self.graph().resume(caught.exception.run_id)
        self.assertEqual(llm.call_count, 1)
        self.assertTrue(result["memory_persisted"])
        self.assertTrue(result["validation"]["valid"])
        self.assertNotIn("SECRET", json.dumps(get_run_trace(result["run_id"])))

    def test_comparison_requires_same_retrieval_and_memory_inputs(self):
        native = LawReviewCoordinator(self.case_id, False).process_query("固定回报", use_llm=False, persist_memory=False, memory_snapshot=[])
        graph = self.graph().process_query("固定回报", use_llm=False, persist_memory=False, memory_snapshot=[])
        self.assertTrue(compare_runtime_results(native, graph)["equivalent"])
        graph["retrieval_metrics"] = {**graph["retrieval_metrics"], "vector_candidates": -1}
        self.assertFalse(compare_runtime_results(native, graph)["equivalent"])


class CaseEvaluationTest(IsolatedQualityTest):
    def truth(self):
        return [{"query": "固定回报", "expected": [["流水.txt", 1]]}]

    def test_non_demo_case_requires_own_truth(self):
        with self.assertRaisesRegex(ValueError, "非演示案件"):
            evaluate_case(self.case_id, False)

    def test_case_specific_truth_is_validated_against_its_sources(self):
        report = evaluate_case(self.case_id, False, ground_truth=self.truth())
        self.assertEqual(report["recall_at_k"], 1.0)
        self.assertEqual(report["quote_presence_rate"], 1.0)
        self.assertIsNone(report["answer_citation_faithfulness"])
        self.assertEqual(report["scope"], "page_retrieval_only")
        for expected in ([["other-case.txt", 1]], [["流水.txt", 999]], [["流水.txt", True]]):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                evaluate_case(self.case_id, False, ground_truth=[{"query": "问题", "expected": expected}])

    def test_no_answer_questions_do_not_inflate_recall(self):
        report = evaluate_case(self.case_id, False, ground_truth=[{"query": "固定回报", "expected": []}])
        self.assertIsNone(report["recall_at_k"])
        self.assertEqual(report["unanswerable_empty_rate"], 0.0)
        self.assertIsNone(platform_metrics(self.case_id)["evaluation"]["recall_at_k"])

    def test_platform_uses_exact_latest_batch_not_last_four_queries(self):
        evaluate_case(self.case_id, False, ground_truth=self.truth() * 5)
        last = evaluate_case(self.case_id, False, ground_truth=self.truth())
        metrics = platform_metrics(self.case_id)
        self.assertEqual(metrics["evaluation"]["queries"], 1)
        self.assertEqual(metrics["evaluation"]["evaluation_id"], last["evaluation_id"])

    def test_failed_eval_does_not_publish_partial_batch(self):
        with patch.object(HybridRetriever, "retrieve", side_effect=[([self.context], {}), RuntimeError("retrieval failed")]):
            with self.assertRaises(RuntimeError):
                evaluate_case(self.case_id, False, ground_truth=self.truth() * 2)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rag_evaluations").fetchone()[0], 0)


class OrdinaryChatQualityTest(IsolatedQualityTest):
    def test_plain_chat_uses_the_same_validation_gate(self):
        with patch("app.services.call_local_llm", return_value="SECRET invented result without citation"), \
                self.assertRaisesRegex(RuntimeError, "model_unavailable_or_invalid"):
            chat(self.case_id, "固定回报", "律师", None, True, False)
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)

    def test_plain_chat_valid_llm_answer_is_used(self):
        answer = "固定回报已经确认[资料1]，请律师复核。"
        with patch("app.services.call_local_llm", return_value=answer):
            result = chat(self.case_id, "固定回报", "律师", None, True, False)
        self.assertTrue(result["llm_used"])
        self.assertEqual(result["answer"], answer)
        self.assertEqual(result["citation_check"], "passed")

    def test_plain_chat_error_body_is_not_persisted_or_returned(self):
        with patch("app.services.call_local_llm", side_effect=RuntimeError("SECRET_TOKEN")), \
                self.assertRaises(RuntimeError) as raised:
            chat(self.case_id, "固定回报", "律师", None, True, False)
        self.assertNotIn("SECRET_TOKEN", str(raised.exception))
        with closing(connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)

    def test_comparison_rule_answer_has_actual_citation_markers(self):
        result = chat(self.case_id, "固定回报的陈述是否矛盾", "律师", None, False, False)
        self.assertEqual(result["route"], "多文档对比")
        self.assertTrue(result["validation"]["valid"])
        self.assertIn("[资料1]", result["answer"])

    def test_directory_statistics_are_labeled_metadata_not_citation_verification(self):
        result = chat(self.case_id, "共有多少份卷宗", "律师", None, False, False)
        self.assertEqual(result["validation"]["scope"], "database_metadata_counts")
        self.assertEqual(result["citation_check"], "not_applicable")


class ResponseDeadlineTest(unittest.TestCase):
    def test_stalled_socket_read_after_keepalive_obeys_remaining_deadline(self):
        reader, writer = socket.socketpair()
        with reader, writer:
            reader.settimeout(1)
            writer.sendall(b" ")
            with reader.makefile("rb") as stream:
                response = SimpleNamespace(fp=stream, read1=stream.read1, close=stream.close)
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    read_json_with_deadline(response, 0.05)
                self.assertLess(time.monotonic() - started, 0.5)

    def test_oversized_response_is_rejected(self):
        response = io.BytesIO(b'{"response": "too much content"}')
        with self.assertRaisesRegex(ValueError, "大小限制"):
            read_json_with_deadline(response, 1, max_bytes=5)
        self.assertTrue(response.closed)

    def test_headers_consume_the_same_request_budget(self):
        response = io.BytesIO(b'{"choices":[{"message":{"content":"late"}}]}')
        with patch("app.services.local_llm_available", return_value=(True, "fake-model")), patch(
            "app.services.urllib.request.build_opener"
        ) as opener, patch("app.services.time.monotonic", side_effect=[0.0, 1.1]):
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(RuntimeError, "TimeoutError"):
                call_local_llm("问题", "语义检索", [], timeout=1)


if __name__ == "__main__":
    unittest.main()
