"""Agent one-shot model fallback (A09.1): same scope, same contract, one retry."""
import json
import os
import unittest
from unittest.mock import patch

from tests.support import IsolatedDatabaseTestCase

from app import agents, services
from app.agents import CriticAgent
from app.db import connect, init_db, now, transaction
from app.model_router import FallbackConfigError

CONTEXTS = [
    {
        "document_id": 1, "page_no": 1, "name": "01-起诉意见书.txt", "doc_type": "书证",
        "quote": "2023年3月起宣传年化12%的固定回报并承诺保本保息。",
    },
    {
        "document_id": 1, "page_no": 2, "name": "01-起诉意见书.txt", "doc_type": "书证",
        "quote": "张某审批了上述宣传口径。",
    },
]
PRIMARY_OK = "主模型：张某审批了固定回报宣传[资料1]。请律师复核原文与上述结论。"
PRIMARY_BAD = "主模型给出的结论没有任何来源标记。"
FALLBACK_OK = "回退模型：张某审批了固定回报宣传[资料1]。请律师复核原文与上述结论。"
FALLBACK_BAD = "回退模型同样没有通过契约。"

FALLBACK_ENV = {
    "LAW_REVIEW_LLM_FALLBACK": "on",
    "LAW_REVIEW_LLM_FALLBACK_URL": "http://127.0.0.1:1/v1",
    "LAW_REVIEW_LLM_FALLBACK_MODEL": "fallback-model",
}


class AgentFallbackTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def setUp(self):
        # Tool loop off: this package is about the model fallback, not tools.
        environment = patch.dict(os.environ, {"LAW_REVIEW_TOOL_LOOP": "off", **FALLBACK_ENV})
        environment.start()
        self.addCleanup(environment.stop)
        with connect() as conn:
            conn.execute("DELETE FROM llm_calls")
            conn.execute("DELETE FROM agent_runs")
        # A real run row: llm_calls.run_id is a foreign key, and accounting is
        # only meaningful when the attribution target exists.
        with transaction() as conn:
            self.run_id = int(conn.execute(
                "INSERT INTO agent_runs(case_id, question, route, status, runtime, context_json, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (1, "张某是否批准了宣传？", "事实检索", "running", "native", "{}", now()),
            ).lastrowid)

    def _critic(self) -> CriticAgent:
        agent = CriticAgent(1)
        agent.run_id = self.run_id
        agent.timeout = 30
        # Pin the primary model so the accounting assertion does not depend on the
        # host's LAW_REVIEW_AGENT_CRITIC_MODEL.
        agent.model = "test-model"
        return agent

    def _calls(self) -> list[tuple[str, str, str]]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT kind, status, model FROM llm_calls ORDER BY id"
            ).fetchall()
        return [tuple(row) for row in rows]

    def _answer_with(self, primary: str, fallback: str | None = None):
        """Patch the single-shot model with a per-target script.

        The real ``call_local_llm`` publishes its observation in a ContextVar;
        the substitute must do the same or the accounting row cannot name the model.
        """
        def fake_call(question, route, contexts, **kwargs):
            model = kwargs.get("model_override") or "test-model"
            services._last_llm_usage.set({
                "model": model, "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "ttft_ms": None, "latency_ms": 1, "kind": "critic",
            })
            if kwargs.get("base_url_override"):
                if fallback is None:
                    raise AssertionError("the fallback must not be called")
                return fallback
            return primary

        return patch.object(agents, "call_local_llm", side_effect=fake_call)

    def test_primary_success_never_touches_the_fallback(self):
        with self._answer_with(PRIMARY_OK):
            result = self._critic().run("张某是否批准了宣传？", "事实检索", CONTEXTS, {}, True)
        self.assertTrue(result["llm_used"])
        self.assertEqual(result["answer"], PRIMARY_OK)
        self.assertIsNone(result["model_route"])
        self.assertEqual(self._calls(), [("critic", "ok", "test-model")])

    def test_primary_contract_failure_retries_once_and_records_both_calls(self):
        with self._answer_with(PRIMARY_BAD, FALLBACK_OK):
            result = self._critic().run("张某是否批准了宣传？", "事实检索", CONTEXTS, {}, True)
        self.assertTrue(result["llm_used"])
        self.assertEqual(result["answer"], FALLBACK_OK)
        self.assertEqual(result["model_route"]["reason"], "primary_failed_validation")
        self.assertEqual(result["model_route"]["model"], "fallback-model")
        self.assertEqual(result["citation_check"], "passed")
        self.assertIsNotNone(result["rejected_llm_validation"])
        self.assertEqual(self._calls(), [
            ("critic", "failed", "test-model"),
            ("critic_fallback", "ok", "fallback-model"),
        ])

    def test_second_failure_falls_back_to_the_rule_summary(self):
        with self._answer_with(PRIMARY_BAD, FALLBACK_BAD):
            result = self._critic().run("张某是否批准了宣传？", "事实检索", CONTEXTS, {}, True)
        self.assertFalse(result["llm_used"])
        self.assertIn("均未通过", result["fallback_reason"])
        self.assertEqual(result["failure_diagnostic"]["phase"], "critic_validation")
        self.assertEqual(result["model_route"]["validation_valid"], False)
        self.assertEqual(self._calls(), [
            ("critic", "failed", "test-model"),
            ("critic_fallback", "failed", "fallback-model"),
        ])

    def test_routing_off_keeps_the_old_single_attempt(self):
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_FALLBACK": "off"}), \
                self._answer_with(PRIMARY_BAD):
            result = self._critic().run("张某是否批准了宣传？", "事实检索", CONTEXTS, {}, True)
        self.assertFalse(result["llm_used"])
        self.assertIsNone(result["model_route"])
        self.assertIn("未通过格式/来源校验", result["fallback_reason"])
        self.assertEqual(len(self._calls()), 1)

    def test_half_configured_fallback_fails_closed_and_says_why(self):
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_FALLBACK_MODEL": ""}), \
                self._answer_with(PRIMARY_BAD):
            result = self._critic().run("张某是否批准了宣传？", "事实检索", CONTEXTS, {}, True)
        self.assertFalse(result["llm_used"])
        self.assertEqual(result["model_route"]["reason"], "fallback_misconfigured")
        # Only the primary was called; the misconfiguration did not become a call.
        self.assertEqual(len(self._calls()), 1)

    def test_disallowed_remote_fallback_endpoint_is_refused(self):
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_FALLBACK_URL": "http://203.0.113.9/v1"}), \
                self._answer_with(PRIMARY_BAD):
            result = self._critic().run("张某是否批准了宣传？", "事实检索", CONTEXTS, {}, True)
        self.assertFalse(result["llm_used"])
        self.assertEqual(result["model_route"]["reason"], "fallback_misconfigured")
        self.assertIn("回退端点不被允许", result["model_route"]["detail"])
        self.assertEqual(len(self._calls()), 1)

    def test_fallback_call_failure_is_reported_without_a_second_retry(self):
        def fake_call(question, route, contexts, **kwargs):
            services._last_llm_usage.set({
                "model": kwargs.get("model_override") or "test-model",
                "usage": None, "ttft_ms": None, "latency_ms": 1, "kind": "critic",
            })
            if kwargs.get("base_url_override"):
                raise RuntimeError("回退端点不可用")
            return PRIMARY_BAD

        with patch.object(agents, "call_local_llm", side_effect=fake_call):
            result = self._critic().run("张某是否批准了宣传？", "事实检索", CONTEXTS, {}, True)
        self.assertFalse(result["llm_used"])
        self.assertEqual(result["model_route"]["reason"], "fallback_failed")
        self.assertEqual(self._calls(), [
            ("critic", "failed", "test-model"),
            ("critic_fallback", "failed", "fallback-model"),
        ])

    def test_fallback_answer_still_uses_the_same_citation_contract(self):
        """A fallback answer citing an out-of-range source is rejected, not trusted."""
        out_of_range = "回退模型引用了不存在的来源[资料99]。请律师复核。"
        with self._answer_with(PRIMARY_BAD, out_of_range):
            result = self._critic().run("张某是否批准了宣传？", "事实检索", CONTEXTS, {}, True)
        self.assertFalse(result["llm_used"])
        self.assertFalse(result["model_route"]["validation_valid"])
        self.assertIn("均未通过", result["fallback_reason"])
        # The published answer is the deterministic rule summary, not either draft.
        self.assertIn("审校说明", result["answer"])
        self.assertNotIn("资料99", result["answer"])

    def test_path_without_run_attribution_skips_the_fallback(self):
        with patch.dict(os.environ, FALLBACK_ENV), self._answer_with(PRIMARY_BAD):
            output = CriticAgent(None).run("问题", "事实检索", CONTEXTS, {}, True)
        self.assertFalse(output["llm_used"])
        self.assertEqual(output["model_route"]["reason"], "run_attribution_unavailable")

    def test_fallback_configuration_error_is_a_value_error(self):
        """The router's error type stays a ValueError so callers can map it."""
        self.assertTrue(issubclass(FallbackConfigError, ValueError))


class LangGraphCriticAttributionTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def test_langgraph_critic_disables_tools_by_policy_and_keeps_attribution(self):
        environment = patch.dict(os.environ, {"LAW_REVIEW_TOOL_LOOP": "on", **FALLBACK_ENV})
        environment.start()
        self.addCleanup(environment.stop)
        with connect() as conn:
            conn.execute("DELETE FROM llm_calls")
        with transaction() as conn:
            run_id = int(conn.execute(
                "INSERT INTO agent_runs(case_id, question, route, status, runtime, context_json, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (1, "问题", "事实检索", "running", "langgraph", "{}", now()),
            ).lastrowid)
        state = {"case_id": 1, "run_id": run_id, "question": "问题", "route": "事实检索",
                 "contexts": CONTEXTS, "specialist_outputs": {}, "use_llm": True}
        from app.langgraph_agents import LangGraphCoordinator

        coordinator = LangGraphCoordinator(1)
        def fake_call(question, route, contexts, **kwargs):
            services._last_llm_usage.set({
                "model": "test-model", "usage": None, "ttft_ms": None, "latency_ms": 1,
                "kind": "critic",
            })
            return PRIMARY_OK

        with patch.object(agents, "call_local_llm", side_effect=fake_call):
            result = coordinator._critic_node(state)
        critic = result["critic"]
        self.assertEqual(critic["tools"]["reason"], "runtime_policy")
        self.assertTrue(critic["llm_used"])
        with connect() as conn:
            rows = conn.execute("SELECT kind, run_id FROM llm_calls").fetchall()
        # The critic call is now attributed to the LangGraph run.
        self.assertEqual([tuple(row) for row in rows], [("critic", run_id)])

    def test_langgraph_result_exposes_the_model_route(self):
        environment = patch.dict(os.environ, {"LAW_REVIEW_TOOL_LOOP": "off"})
        environment.start()
        self.addCleanup(environment.stop)
        with patch.object(agents, "call_local_llm", return_value=PRIMARY_OK):
            output = CriticAgent(1, allow_tool_loop=False).run(
                "问题", "事实检索", CONTEXTS, {}, True
            )
        self.assertEqual(output["tools"]["reason"], "runtime_policy")
        self.assertIsNone(output["model_route"])


if __name__ == "__main__":
    unittest.main()
