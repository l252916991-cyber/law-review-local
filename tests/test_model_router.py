"""Fallback routing: one bounded retry, same contract, explicit configuration."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.llm.model_router import FallbackConfigError, fallback_target
from app.storage.db import db_scope, init_db


class FallbackConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LAW_REVIEW_LLM_FALLBACK", None)
            self.assertIsNone(fallback_target())

    def test_enabled_without_url_or_model_raises(self):
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_FALLBACK": "on", "LAW_REVIEW_LLM_FALLBACK_URL": ""}):
            with self.assertRaises(FallbackConfigError):
                fallback_target()

    def test_remote_plain_http_is_refused(self):
        env = {
            "LAW_REVIEW_LLM_FALLBACK": "on",
            "LAW_REVIEW_LLM_FALLBACK_URL": "http://example.com/v1",
            "LAW_REVIEW_LLM_FALLBACK_MODEL": "m",
        }
        with patch.dict(os.environ, env):
            with self.assertRaises(FallbackConfigError):
                fallback_target()

    def test_loopback_target_is_accepted(self):
        env = {
            "LAW_REVIEW_LLM_FALLBACK": "on",
            "LAW_REVIEW_LLM_FALLBACK_URL": "http://127.0.0.1:8001/v1",
            "LAW_REVIEW_LLM_FALLBACK_MODEL": "big-model",
        }
        with patch.dict(os.environ, env):
            target = fallback_target()
        assert target is not None
        self.assertEqual(target.model, "big-model")
        self.assertEqual(target.base_url, "http://127.0.0.1:8001/v1")
        # The target states its own protocol instead of inheriting the primary's
        # transport code: a fallback commonly runs on a different backend.
        self.assertEqual(target.api, "openai-completions")

    def test_fallback_declares_its_own_protocol(self):
        env = {
            "LAW_REVIEW_LLM_FALLBACK": "on",
            "LAW_REVIEW_LLM_FALLBACK_URL": "http://127.0.0.1:8001",
            "LAW_REVIEW_LLM_FALLBACK_MODEL": "claude",
            "LAW_REVIEW_LLM_FALLBACK_API": "anthropic-messages",
        }
        with patch.dict(os.environ, env):
            target = fallback_target()
        assert target is not None
        self.assertEqual(target.api, "anthropic-messages")

    def test_unknown_fallback_protocol_is_refused(self):
        env = {
            "LAW_REVIEW_LLM_FALLBACK": "on",
            "LAW_REVIEW_LLM_FALLBACK_URL": "http://127.0.0.1:8001",
            "LAW_REVIEW_LLM_FALLBACK_MODEL": "m",
            "LAW_REVIEW_LLM_FALLBACK_API": "grpc",
        }
        with patch.dict(os.environ, env):
            with self.assertRaises(FallbackConfigError):
                fallback_target()


class FallbackRoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-router-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        self.env = {
            "LAW_REVIEW_LLM_FALLBACK": "on",
            "LAW_REVIEW_LLM_FALLBACK_URL": "http://127.0.0.1:8001/v1",
            "LAW_REVIEW_LLM_FALLBACK_MODEL": "big-model",
        }

    def _chat(self, answers):
        from app.review.chat import chat

        calls = []

        def fake(question, route, contexts, **kwargs):
            calls.append(kwargs.get("model_override") or "primary")
            return answers[len(calls) - 1]

        with patch.dict(os.environ, self.env), patch("app.review.chat.call_local_llm", side_effect=fake):
            result = chat(1, "张某是否批准了年化12%的固定回报宣传？", "本机律师", None, True, False)
        return result, calls

    def test_invalid_primary_answer_is_replaced_by_a_valid_fallback(self):
        result, calls = self._chat([
            "没有引用的草稿。",  # fails the citation contract
            "根据材料 [资料1]。\n\n请律师复核原文与上述结论。",
        ])
        self.assertTrue(result["llm_used"])
        self.assertEqual(calls, ["primary", "big-model"])
        self.assertTrue(result["provenance"]["route"]["fallback"])
        self.assertEqual(result["provenance"]["route"]["reason"], "primary_failed_validation")
        self.assertIsNotNone(result["rejected_llm_validation"])

    def test_valid_primary_answer_never_calls_the_fallback(self):
        result, calls = self._chat(["根据材料 [资料1]。\n\n请律师复核原文与上述结论。"])
        self.assertEqual(calls, ["primary"])
        self.assertNotIn("route", result["provenance"])

    def test_fallback_that_also_fails_keeps_the_request_failed(self):
        from app.review.chat import chat

        with patch.dict(os.environ, self.env), patch(
            "app.review.chat.call_local_llm", side_effect=["草稿一", "草稿二"]
        ):
            with self.assertRaises(RuntimeError):
                chat(1, "张某是否批准了年化12%的固定回报宣传？", "本机律师", None, True, False)

    def test_routing_stays_off_without_the_flag(self):
        from app.review.chat import chat

        env = dict(self.env)
        env["LAW_REVIEW_LLM_FALLBACK"] = "off"
        with patch.dict(os.environ, env), patch(
            "app.review.chat.call_local_llm", side_effect=["没有引用的草稿。"]
        ):
            with self.assertRaises(RuntimeError):
                chat(1, "张某是否批准了年化12%的固定回报宣传？", "本机律师", None, True, False)


if __name__ == "__main__":
    unittest.main()
