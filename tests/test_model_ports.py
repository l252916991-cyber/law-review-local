"""Model layer: provider/wire-api split, reasoning handoff, scripted backend.

These tests pin the contract that makes a second model backend a registration
rather than a second transport: one provider level, one api level, model data
that can be recorded, and credentials that never reach a log or a provenance
record.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import model_ports
from app.model_ports import (
    ANTHROPIC_API_VERSION,
    MODEL_API_ANTHROPIC_MESSAGES,
    MODEL_API_OPENAI_COMPLETIONS,
    EnvProvider,
    MissingEndpointError,
    ModelPortError,
    Models,
    ModelSpec,
    ProviderAuth,
    ScriptedProvider,
    ScriptedQueueEmpty,
    ScriptedReply,
    UnknownModelError,
    UnsupportedModelApiError,
    auth_headers,
    build_chat_request,
    configured_api,
    drop_thinking_blocks,
    end_frame_types,
    handoff_messages,
    load_catalog,
    load_credentials,
    parse_catalog,
    parse_completion,
    parse_stream_frame,
    request_path,
    strip_thinking_text,
)


def _spec(model_id: str = "m", *, provider: str = "local", api: str = MODEL_API_OPENAI_COMPLETIONS,
          **extra) -> ModelSpec:
    return ModelSpec(id=model_id, api=api, provider=provider, **extra)


class ModelSpecTests(unittest.TestCase):
    def test_defaults_mark_an_undeclared_mid_size_model(self):
        spec = ModelSpec(id="m")
        self.assertEqual(spec.api, MODEL_API_OPENAI_COMPLETIONS)
        self.assertIsNone(spec.context_window)
        self.assertFalse(spec.supports_tools)
        self.assertFalse(spec.supports_vision)
        self.assertIsNone(spec.price_column)

    def test_rejects_an_empty_id(self):
        with self.assertRaises(ModelPortError):
            ModelSpec(id="   ")

    def test_rejects_an_unknown_api(self):
        with self.assertRaises(UnsupportedModelApiError):
            ModelSpec(id="m", api="grpc")

    def test_rejects_a_boolean_window(self):
        # ``True`` is an ``int``; a flag must never become a context window.
        with self.assertRaises(ModelPortError):
            ModelSpec(id="m", context_window=True)  # type: ignore[arg-type]

    def test_rejects_a_non_positive_window(self):
        with self.assertRaises(ModelPortError):
            ModelSpec(id="m", max_output_tokens=0)

    def test_round_trips_through_dict(self):
        spec = _spec("qwen", context_window=32768, max_output_tokens=900,
                     supports_tools=True, supports_vision=True, supports_reasoning=True,
                     price_column="snap-1")
        self.assertEqual(ModelSpec.from_dict(spec.to_dict()), spec)

    def test_from_dict_rejects_a_non_boolean_flag(self):
        with self.assertRaises(ModelPortError):
            ModelSpec.from_dict({"id": "m", "supports_tools": "yes"})

    def test_from_dict_rejects_a_non_object(self):
        with self.assertRaises(ModelPortError):
            ModelSpec.from_dict(["m"])  # type: ignore[arg-type]

    def test_from_dict_ignores_an_empty_price_column(self):
        self.assertIsNone(ModelSpec.from_dict({"id": "m", "price_column": ""}).price_column)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.local = EnvProvider(name="local", base_url="http://127.0.0.1:8000/v1",
                                 model_specs=(_spec("qwen"), _spec("qwen-vision", supports_vision=True)))
        self.backup = EnvProvider(name="backup", base_url="http://10.0.0.5:8080/v1",
                                  api=MODEL_API_ANTHROPIC_MESSAGES,
                                  model_specs=(_spec("claude", provider="backup",
                                                     api=MODEL_API_ANTHROPIC_MESSAGES),))
        self.models = Models([self.local, self.backup])

    def test_lookup_is_across_providers(self):
        self.assertEqual(self.models.get_model("claude").provider, "backup")  # type: ignore[union-attr]
        self.assertIsNone(self.models.get_model("missing"))
        self.assertIsNone(self.models.get_model(""))

    def test_available_is_ordered_and_complete(self):
        ids = [spec.id for spec in self.models.get_available()]
        self.assertEqual(ids, ["claude", "qwen", "qwen-vision"])

    def test_declared_model_resolves_its_provider_and_api(self):
        target = self.models.resolve("claude")
        self.assertEqual(target.endpoint, "http://10.0.0.5:8080/v1")
        self.assertEqual(target.api, MODEL_API_ANTHROPIC_MESSAGES)
        self.assertTrue(target.declared)

    def test_explicit_endpoint_overrides_the_provider(self):
        target = self.models.resolve("qwen", base_url="http://127.0.0.1:9999/v1/")
        self.assertEqual(target.endpoint, "http://127.0.0.1:9999/v1")

    def test_explicit_provider_selects_the_backend(self):
        target = self.models.resolve("qwen", provider="backup")
        self.assertEqual(target.endpoint, "http://10.0.0.5:8080/v1")

    def test_api_override_wins_over_the_declared_spec(self):
        target = self.models.resolve("qwen", api=MODEL_API_ANTHROPIC_MESSAGES)
        self.assertEqual(target.api, MODEL_API_ANTHROPIC_MESSAGES)
        self.assertTrue(target.declared)

    def test_undeclared_model_is_refused_by_default(self):
        with self.assertRaises(UnknownModelError):
            self.models.resolve("brand-new")

    def test_undeclared_model_is_allowed_and_marked(self):
        target = self.models.resolve("brand-new", base_url="http://127.0.0.1:8000/v1",
                                     allow_undeclared=True)
        self.assertFalse(target.declared)
        self.assertEqual(target.spec.api, MODEL_API_OPENAI_COMPLETIONS)

    def test_unknown_api_is_refused(self):
        with self.assertRaises(UnsupportedModelApiError):
            self.models.resolve("qwen", api="xmlrpc")

    def test_empty_model_id_is_refused(self):
        with self.assertRaises(UnknownModelError):
            self.models.resolve("  ")

    def test_missing_endpoint_is_refused(self):
        models = Models([EnvProvider(name="local", base_url="")])
        with self.assertRaises(MissingEndpointError):
            models.resolve("qwen", allow_undeclared=True)

    def test_unregistered_provider_without_endpoint_is_refused(self):
        with self.assertRaises(MissingEndpointError):
            self.models.resolve("qwen", provider="ghost", allow_undeclared=True)

    def test_single_provider_registry_is_the_default_target(self):
        # An undeclared model must still resolve the one configured provider so
        # its credentials and identity are not lost to an invented backend.
        models = Models([EnvProvider(name="local", base_url="http://127.0.0.1:8000/v1",
                                     stored_api_key="sk-stored")])
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_API_KEY": ""}):
            target = models.resolve("undeclared", allow_undeclared=True)
        self.assertEqual(target.spec.provider, "local")
        self.assertEqual(target.auth.api_key, "sk-stored")
        self.assertEqual(target.auth.source, "stored")

    def test_two_providers_make_the_caller_choose(self):
        models = Models([
            EnvProvider(name="local", base_url="http://127.0.0.1:8000/v1"),
            EnvProvider(name="other", base_url="http://127.0.0.1:8001/v1"),
        ])
        with self.assertRaises(MissingEndpointError):
            models.resolve("undeclared", allow_undeclared=True)

    def test_price_column_is_filled_from_the_snapshot_lookup(self):
        target = self.models.resolve("qwen", price_column_for=lambda model: f"snap:{model}")
        self.assertEqual(target.spec.price_column, "snap:qwen")

    def test_provenance_carries_identity_and_never_the_key(self):
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_API_KEY": "sk-secret"}):
            target = self.models.resolve("qwen")
            record = target.provenance()
        self.assertEqual(record["model"], "qwen")
        self.assertEqual(record["api"], MODEL_API_OPENAI_COMPLETIONS)
        self.assertEqual(record["provider"], "local")
        self.assertTrue(record["declared"])
        self.assertEqual(record["auth_source"], "env")
        self.assertNotIn("sk-secret", json.dumps(record))

    def test_configured_api_rejects_an_unknown_value(self):
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_API": "carrier-pigeon"}):
            with self.assertRaises(UnsupportedModelApiError):
                configured_api()
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_API": ""}):
            self.assertEqual(configured_api(), MODEL_API_OPENAI_COMPLETIONS)


class AuthTests(unittest.TestCase):
    def test_openai_uses_a_bearer_token(self):
        headers = auth_headers(MODEL_API_OPENAI_COMPLETIONS, ProviderAuth("http://h/v1", "sk-1", "env"))
        self.assertEqual(headers["Authorization"], "Bearer sk-1")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_anthropic_uses_x_api_key_and_a_version(self):
        headers = auth_headers(MODEL_API_ANTHROPIC_MESSAGES, ProviderAuth("http://h", "sk-2", "env"))
        self.assertEqual(headers["x-api-key"], "sk-2")
        self.assertEqual(headers["anthropic-version"], ANTHROPIC_API_VERSION)
        self.assertNotIn("Authorization", headers)

    def test_no_key_means_no_auth_header(self):
        for api in (MODEL_API_OPENAI_COMPLETIONS, MODEL_API_ANTHROPIC_MESSAGES):
            headers = auth_headers(api, ProviderAuth("http://h"))
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("x-api-key", headers)

    def test_unknown_api_has_no_headers(self):
        with self.assertRaises(UnsupportedModelApiError):
            auth_headers("soap", ProviderAuth("http://h"))

    def test_redacted_never_contains_the_key(self):
        redacted = ProviderAuth("http://h", "sk-3", "env").redacted()
        self.assertEqual(redacted["api_key"], "set")
        self.assertEqual(redacted["source"], "env")
        self.assertNotIn("sk-3", json.dumps(redacted))

    def test_redacted_marks_an_absent_key(self):
        self.assertEqual(ProviderAuth("http://h").redacted()["api_key"], "unset")

    def test_env_key_wins_over_the_stored_one(self):
        provider = EnvProvider(name="local", base_url="http://h", stored_api_key="sk-stored")
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_API_KEY": "sk-env"}):
            auth = provider.auth()
        self.assertEqual(auth.api_key, "sk-env")
        self.assertEqual(auth.source, "env")

    def test_stored_key_is_used_when_the_env_is_unset(self):
        provider = EnvProvider(name="local", base_url="http://h", stored_api_key="sk-stored")
        with patch.dict(os.environ, {"LAW_REVIEW_LLM_API_KEY": ""}):
            auth = provider.auth()
        self.assertEqual(auth.api_key, "sk-stored")
        self.assertEqual(auth.source, "stored")

    def test_base_url_is_normalised(self):
        self.assertEqual(EnvProvider(name="local", base_url="http://h/v1///").auth().base_url,
                         "http://h/v1")


class CredentialFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-creds-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.path.write_text(json.dumps({"providers": {"local": {"api_key": "sk-file"}}}), encoding="utf-8")
        self.path.chmod(0o600)

    def test_secure_file_is_loaded(self):
        self.assertEqual(load_credentials(self.path), {"local": "sk-file"})

    def test_file_visible_to_the_group_is_refused(self):
        self.path.chmod(0o644)
        self.assertEqual(load_credentials(self.path), {})

    def test_malformed_file_is_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_credentials(self.path), {})

    def test_structure_without_providers_is_empty(self):
        self.path.write_text(json.dumps({"key": "sk-x"}), encoding="utf-8")
        self.assertEqual(load_credentials(self.path), {})

    def test_unset_variable_reads_nothing(self):
        with patch.dict(os.environ, {"LAW_REVIEW_MODEL_CREDENTIALS_FILE": ""}):
            self.assertEqual(load_credentials(), {})

    def test_variable_points_at_the_file(self):
        with patch.dict(os.environ, {"LAW_REVIEW_MODEL_CREDENTIALS_FILE": str(self.path)}):
            self.assertEqual(load_credentials(), {"local": "sk-file"})


class CatalogTests(unittest.TestCase):
    def test_bare_list_is_parsed(self):
        specs = parse_catalog([{"id": "a"}, {"id": "b", "api": MODEL_API_ANTHROPIC_MESSAGES}])
        self.assertEqual([spec.id for spec in specs], ["a", "b"])

    def test_provider_is_filled_from_the_enclosing_scope(self):
        specs = parse_catalog({"models": [{"id": "a"}]}, provider="local")
        self.assertEqual(specs[0].provider, "local")

    def test_object_without_models_is_refused(self):
        with self.assertRaises(ModelPortError):
            parse_catalog({"models": "a"})

    def test_absent_configuration_declares_nothing(self):
        with patch.dict(os.environ, {"LAW_REVIEW_MODEL_CATALOG": "",
                                     "LAW_REVIEW_MODEL_CATALOG_FILE": ""}):
            self.assertEqual(load_catalog(), ())

    def test_inline_catalog_is_parsed(self):
        payload = json.dumps({"models": [{"id": "qwen", "context_window": 32768}]})
        with patch.dict(os.environ, {"LAW_REVIEW_MODEL_CATALOG": payload,
                                     "LAW_REVIEW_MODEL_CATALOG_FILE": ""}):
            specs = load_catalog()
        self.assertEqual(specs[0].id, "qwen")
        self.assertEqual(specs[0].context_window, 32768)

    def test_broken_catalog_is_a_hard_error(self):
        # A deployment that believes it declared capabilities must not run
        # without them; silently degrading to "undeclared" hides the mistake.
        with patch.dict(os.environ, {"LAW_REVIEW_MODEL_CATALOG": "{broken",
                                     "LAW_REVIEW_MODEL_CATALOG_FILE": ""}):
            with self.assertRaises(ModelPortError):
                load_catalog()


class WireFormatTests(unittest.TestCase):
    def test_openai_body_is_unchanged_from_the_single_endpoint_client(self):
        body = build_chat_request(api=MODEL_API_OPENAI_COMPLETIONS, model="qwen",
                                  system="S", user="U", temperature=0.2, max_tokens=900,
                                  stream=False)
        self.assertEqual(body, {
            "model": "qwen",
            "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
            "temperature": 0.2,
            "max_tokens": 900,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        })

    def test_openai_stream_asks_for_usage(self):
        body = build_chat_request(api=MODEL_API_OPENAI_COMPLETIONS, model="qwen",
                                  system="S", user="U", temperature=0.2, max_tokens=900,
                                  stream=True)
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_anthropic_body_uses_the_system_field(self):
        body = build_chat_request(api=MODEL_API_ANTHROPIC_MESSAGES, model="claude",
                                  system="S", user="U", temperature=0.2, max_tokens=900,
                                  stream=False)
        self.assertEqual(body["system"], "S")
        self.assertEqual(body["messages"], [{"role": "user", "content": "U"}])
        self.assertEqual(body["max_tokens"], 900)
        self.assertNotIn("stream_options", body)
        self.assertNotIn("thinking", body)

    def test_anthropic_body_omits_an_empty_system_and_adds_thinking_on_request(self):
        body = build_chat_request(api=MODEL_API_ANTHROPIC_MESSAGES, model="claude",
                                  system="", user="U", temperature=0.0, max_tokens=10,
                                  stream=True, thinking=True)
        self.assertNotIn("system", body)
        self.assertEqual(body["thinking"], {"type": "enabled"})

    def test_unknown_api_builds_nothing(self):
        with self.assertRaises(UnsupportedModelApiError):
            build_chat_request(api="smoke-signals", model="m", system="S", user="U",
                               temperature=0.0, max_tokens=1, stream=False)

    def test_paths_and_end_frames_per_api(self):
        self.assertEqual(request_path(MODEL_API_OPENAI_COMPLETIONS), "/chat/completions")
        self.assertEqual(request_path(MODEL_API_ANTHROPIC_MESSAGES), "/messages")
        self.assertEqual(end_frame_types(MODEL_API_OPENAI_COMPLETIONS), frozenset())
        self.assertEqual(end_frame_types(MODEL_API_ANTHROPIC_MESSAGES), frozenset({"message_stop"}))

    def test_openai_completion_is_read_from_message_content(self):
        text, usage = parse_completion(MODEL_API_OPENAI_COMPLETIONS, {
            "choices": [{"message": {"content": "答案[资料1]"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        })
        self.assertEqual(text, "答案[资料1]")
        self.assertEqual(usage, {"prompt_tokens": 9, "completion_tokens": 3})

    def test_openai_completion_without_choices_is_refused(self):
        with self.assertRaises(ModelPortError):
            parse_completion(MODEL_API_OPENAI_COMPLETIONS, {"choices": []})

    def test_anthropic_completion_joins_text_and_skips_reasoning(self):
        text, usage = parse_completion(MODEL_API_ANTHROPIC_MESSAGES, {
            "content": [
                {"type": "thinking", "thinking": "scratchpad"},
                {"type": "text", "text": "答案"},
                {"type": "text", "text": "[资料1]"},
            ],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        })
        self.assertEqual(text, "答案[资料1]")
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 5})

    def test_anthropic_completion_without_content_is_refused(self):
        with self.assertRaises(ModelPortError):
            parse_completion(MODEL_API_ANTHROPIC_MESSAGES, {"type": "message"})

    def test_anthropic_stream_frames(self):
        self.assertEqual(
            parse_stream_frame(MODEL_API_ANTHROPIC_MESSAGES,
                               {"type": "message_start",
                                "message": {"usage": {"input_tokens": 7}}}),
            (None, {"input_tokens": 7}),
        )
        self.assertEqual(
            parse_stream_frame(MODEL_API_ANTHROPIC_MESSAGES,
                               {"type": "content_block_delta",
                                "delta": {"type": "text_delta", "text": "根据"}}),
            ("根据", None),
        )
        self.assertEqual(
            parse_stream_frame(MODEL_API_ANTHROPIC_MESSAGES,
                               {"type": "message_delta", "usage": {"output_tokens": 4}}),
            (None, {"output_tokens": 4}),
        )
        self.assertEqual(
            parse_stream_frame(MODEL_API_ANTHROPIC_MESSAGES,
                               {"type": "content_block_stop"}), (None, None))

    def test_anthropic_thinking_delta_is_never_text(self):
        # A reasoning delta must not reach the citation validator as content.
        self.assertEqual(
            parse_stream_frame(MODEL_API_ANTHROPIC_MESSAGES,
                               {"type": "content_block_delta",
                                "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
            (None, None),
        )

    def test_openai_stream_frame_variants(self):
        self.assertEqual(
            parse_stream_frame(MODEL_API_OPENAI_COMPLETIONS,
                               {"choices": [{"delta": {"content": "a"}}]}), ("a", None))
        self.assertEqual(
            parse_stream_frame(MODEL_API_OPENAI_COMPLETIONS,
                               {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 1}}),
            (None, {"prompt_tokens": 1}))
        self.assertEqual(
            parse_stream_frame(MODEL_API_OPENAI_COMPLETIONS, {"choices": []}),
            (None, None))


class ThinkingHandoffTests(unittest.TestCase):
    def test_same_model_keeps_the_turn_byte_for_byte(self):
        messages = [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "推理", "signature": "sig-1"},
            {"type": "text", "text": "答案"},
        ]}]
        prepared = handoff_messages(messages, source_model="qwen", target_model="qwen")
        self.assertEqual(prepared, messages)
        prepared[0]["content"].append({"type": "text", "text": "mutated"})
        self.assertEqual(len(messages[0]["content"]), 2)

    def test_different_model_drops_thinking_blocks(self):
        messages = [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "推理", "signature": "sig-1"},
            {"type": "redacted_thinking", "data": "x"},
            {"type": "text", "text": "答案"},
        ]}]
        prepared = handoff_messages(messages, source_model="qwen", target_model="claude")
        self.assertEqual(prepared, [{"role": "assistant", "content": [{"type": "text", "text": "答案"}]}])

    def test_reasoning_only_turn_is_dropped_entirely(self):
        messages = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "推理"}]},
        ]
        prepared = handoff_messages(messages, source_model="qwen", target_model="claude")
        self.assertEqual(prepared, [{"role": "user", "content": "问题"}])

    def test_text_tags_are_stripped_from_string_content(self):
        prepared = handoff_messages(
            [{"role": "assistant", "content": "<think>推理</think>答案[资料1]"}],
            source_model="qwen", target_model="claude",
        )
        self.assertEqual(prepared[0]["content"], "答案[资料1]")

    def test_unterminated_reasoning_tail_is_stripped(self):
        self.assertEqual(strip_thinking_text("答案<think >没写完"), "答案")

    def test_text_only_content_is_untouched_across_models(self):
        prepared = handoff_messages([{"role": "user", "content": "原问题"}],
                                    source_model="a", target_model="b")
        self.assertEqual(prepared, [{"role": "user", "content": "原问题"}])

    def test_empty_text_blocks_are_removed(self):
        cleaned = drop_thinking_blocks({"role": "assistant", "content": [
            {"type": "text", "text": "<think>x</think>"},
            {"type": "text", "text": "答案"},
        ]})
        self.assertEqual(cleaned["content"], [{"type": "text", "text": "答案"}])


class ScriptedProviderTests(unittest.TestCase):
    def test_serves_replies_in_order_and_then_refuses(self):
        provider = ScriptedProvider([ScriptedReply("第一", {"prompt_tokens": 1}), "第二"])
        self.assertEqual(provider.remaining, 2)
        self.assertEqual(provider.chat({"model": "scripted-model"}).text, "第一")
        self.assertEqual(provider.chat({}).text, "第二")
        self.assertEqual(provider.remaining, 0)
        with self.assertRaises(ScriptedQueueEmpty):
            provider.chat({})

    def test_requests_are_recorded_as_copies(self):
        provider = ScriptedProvider(["ok"])
        payload = {"model": "scripted-model", "messages": []}
        provider.chat(payload)
        payload["model"] = "tampered"
        self.assertEqual(provider.requests[0]["model"], "scripted-model")

    def test_streaming_yields_chunks_in_order(self):
        provider = ScriptedProvider(["abcdefgh"], chunk_size=2)
        self.assertEqual(list(provider.stream({"messages": []})), ["ab", "cd", "ef", "gh"])

    def test_pacing_goes_through_the_injected_sleep(self):
        pauses: list[float] = []
        provider = ScriptedProvider(["abcdefgh"], chunk_size=2, tokens_per_second=10,
                                    sleep=pauses.append)
        self.assertEqual("".join(provider.stream({})), "abcdefgh")
        self.assertEqual(len(pauses), 4)
        for pause in pauses:
            self.assertAlmostEqual(pause, 0.1)

    def test_no_pacing_means_no_sleep(self):
        pauses: list[float] = []
        provider = ScriptedProvider(["abcd"], chunk_size=2, sleep=pauses.append)
        provider.chat({})
        self.assertEqual(pauses, [])

    def test_rejects_an_unknown_api_and_a_bad_chunk_size(self):
        with self.assertRaises(UnsupportedModelApiError):
            ScriptedProvider(api="pigeon")
        with self.assertRaises(ModelPortError):
            ScriptedProvider(chunk_size=0)
        with self.assertRaises(ModelPortError):
            ScriptedProvider(tokens_per_second=-1)

    def test_is_a_registered_provider(self):
        provider = ScriptedProvider(["ok"], model="fake-1", supports_tools=True)
        models = Models([provider])
        target = models.resolve("fake-1")
        self.assertEqual(target.endpoint, "http://scripted.invalid/v1")
        self.assertTrue(target.spec.supports_tools)
        self.assertTrue(target.declared)
        self.assertEqual(target.auth.source, "none")


class ModuleBoundaryTests(unittest.TestCase):
    def test_model_ports_imports_without_the_application(self):
        # The module must stay usable from a script or a benchmark that has no
        # database: only ``default_models`` reaches into ``app.config``.
        source = Path(model_ports.__file__).read_text(encoding="utf-8")
        self.assertIn("from .config import LLMConfig", source)
        self.assertNotIn("from .db import", source)
        self.assertNotIn("from .services import", source)


if __name__ == "__main__":
    unittest.main()
