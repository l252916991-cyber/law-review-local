"""Model layer split into a *provider* level and a *wire-API* level.

The split follows the design pi-ai documents for itself (packages/ai of
github.com/badlogic/pi-mono, MIT). Only the shape is taken: no source is copied,
no dependency is added, and the transport guards stay where this codebase already
put them. docs/runbook/licenses.md records that distinction.

Two things are deliberately separate:

- **provider** is the running unit. It owns one base URL, its credentials and its
  model catalog. ``local`` (the oMLX server), a fallback endpoint, and a scripted
  test provider are three providers.
- **api** is the wire protocol that shapes one request/response pair:
  ``openai-completions`` (``/chat/completions``, ``choices[].message.content``)
  or ``anthropic-messages`` (``/messages``, ``content[].text``).

Two providers can share one api (any OpenAI-compatible server), and one provider
can serve models with different apis. Keeping the levels apart is what turns a
second backend into a registration instead of a rewrite of the transport guards
(egress approval, redirect refusal, absolute deadlines, SSE size limits) that
already live in ``app/services.py``.

A model is *data*, not behaviour. ``ModelSpec`` is a frozen record with the id,
wire api, context window, capability flags and the price snapshot column, so the
model behind a stored answer can be serialised into the audit trail instead of
being recoverable only from process configuration.

Two rules this module encodes that the old single-endpoint client could not:

1. **Reasoning never crosses a model handoff.** The signatures on a thinking
   block are only valid for the model that produced them, and a plain-text
   reasoning block gets read by the next model as reply content.
   ``handoff_messages`` drops thinking when the model changes and keeps the turn
   byte-for-byte when it does not.
2. **Credentials are provider configuration and never provenance.** ``ProviderAuth``
   exposes ``redacted()`` for anything that is logged or recorded; the key itself
   is only read by ``auth_headers``.

This module imports the standard library only (plus a lazy ``app.config`` import
inside ``default_models``), so it can be exercised without a database, a model
server or FastAPI.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wire APIs
# ---------------------------------------------------------------------------

MODEL_API_OPENAI_COMPLETIONS = "openai-completions"
MODEL_API_ANTHROPIC_MESSAGES = "anthropic-messages"
MODEL_APIS = frozenset({MODEL_API_OPENAI_COMPLETIONS, MODEL_API_ANTHROPIC_MESSAGES})
DEFAULT_MODEL_API = MODEL_API_OPENAI_COMPLETIONS

#: Path appended to the provider base URL for each wire api.
API_PATH: dict[str, str] = {
    MODEL_API_OPENAI_COMPLETIONS: "/chat/completions",
    MODEL_API_ANTHROPIC_MESSAGES: "/messages",
}

#: Frame ``type`` that terminates a stream for an api without a ``[DONE]`` marker.
API_END_FRAME_TYPES: dict[str, frozenset[str]] = {
    MODEL_API_OPENAI_COMPLETIONS: frozenset(),
    MODEL_API_ANTHROPIC_MESSAGES: frozenset({"message_stop"}),
}

ANTHROPIC_API_VERSION = "2023-06-01"

#: Content-block types that carry model-private reasoning rather than answer text.
THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning"})

_THINKING_PATTERN = re.compile(r"<think\b[^>]*>[\s\S]*?</think\s*>", re.IGNORECASE)
_THINKING_OPEN_TAIL = re.compile(r"<think\b[^>]*>[\s\S]*$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Configuration keys
# ---------------------------------------------------------------------------

API_ENV = "LAW_REVIEW_LLM_API"
API_KEY_ENV = "LAW_REVIEW_LLM_API_KEY"
CREDENTIALS_FILE_ENV = "LAW_REVIEW_MODEL_CREDENTIALS_FILE"
CATALOG_ENV = "LAW_REVIEW_MODEL_CATALOG"
CATALOG_FILE_ENV = "LAW_REVIEW_MODEL_CATALOG_FILE"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelPortError(RuntimeError):
    """The model layer cannot be used as configured."""


class UnknownModelError(ModelPortError):
    """An undeclared model was requested where the catalog must answer."""


class UnsupportedModelApiError(ModelPortError):
    """A wire api outside ``MODEL_APIS`` was requested."""


class MissingEndpointError(ModelPortError):
    """A provider has no base URL, so no request could be sent."""


class ScriptedQueueEmpty(ModelPortError):
    """The scripted provider was asked for more replies than it was given."""


# ---------------------------------------------------------------------------
# Model identity
# ---------------------------------------------------------------------------


def _positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    # ``bool`` is an ``int`` subclass; a flag must never become a window size.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelPortError(f"{field_name} 必须是正整数或留空")
    return value


@dataclass(frozen=True)
class ModelSpec:
    """One model as serialisable data.

    ``price_column`` is opaque here on purpose: it is the immutable snapshot JSON
    produced by ``app.pricing.PriceSnapshot.column_for``. This module stays
    stdlib-only, so the pricing rules are not duplicated to carry them.
    """

    id: str
    api: str = DEFAULT_MODEL_API
    provider: str = ""
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = False
    supports_vision: bool = False
    supports_reasoning: bool = False
    price_column: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ModelPortError("模型 ID 不能为空")
        if self.api not in MODEL_APIS:
            raise UnsupportedModelApiError(f"未知的模型 wire api：{self.api}")
        _positive_int(self.context_window, "context_window")
        _positive_int(self.max_output_tokens, "max_output_tokens")
        if self.price_column is not None and not isinstance(self.price_column, str):
            raise ModelPortError("price_column 必须是字符串或留空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "api": self.api,
            "provider": self.provider,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_reasoning": self.supports_reasoning,
            "price_column": self.price_column,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ModelSpec:
        if not isinstance(payload, Mapping):
            raise ModelPortError("模型声明必须是 JSON 对象")
        flags = {}
        for name in ("supports_tools", "supports_vision", "supports_reasoning"):
            value = payload.get(name, False)
            if not isinstance(value, bool):
                raise ModelPortError(f"{name} 必须是布尔值")
            flags[name] = value
        price_column = payload.get("price_column")
        return cls(
            id=str(payload.get("id") or ""),
            api=str(payload.get("api") or DEFAULT_MODEL_API),
            provider=str(payload.get("provider") or ""),
            context_window=_positive_int(payload.get("context_window"), "context_window"),
            max_output_tokens=_positive_int(payload.get("max_output_tokens"), "max_output_tokens"),
            price_column=str(price_column) if isinstance(price_column, str) and price_column else None,
            **flags,
        )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderAuth:
    """Resolved credentials for one provider.

    ``source`` records where the key came from (``env``, ``stored``, ``none``) so
    an operator can tell an unset key from a key that was loaded and rejected.
    """

    base_url: str
    api_key: str | None = None
    source: str = "none"

    def redacted(self) -> dict[str, str]:
        """Everything safe to log or persist: never the key itself."""
        return {
            "base_url": self.base_url,
            "api_key": "set" if self.api_key else "unset",
            "source": self.source,
        }


def auth_headers(api: str, auth: ProviderAuth) -> dict[str, str]:
    """Request headers for one wire api; the only place the key is read out."""
    if api not in MODEL_APIS:
        raise UnsupportedModelApiError(f"未知的模型 wire api：{api}")
    headers = {"Content-Type": "application/json"}
    if api == MODEL_API_ANTHROPIC_MESSAGES:
        headers["anthropic-version"] = ANTHROPIC_API_VERSION
        if auth.api_key:
            headers["x-api-key"] = auth.api_key
    elif auth.api_key:
        headers["Authorization"] = f"Bearer {auth.api_key}"
    return headers


# ---------------------------------------------------------------------------
# Credential file
# ---------------------------------------------------------------------------


def load_credentials(path: str | Path | None = None) -> dict[str, str]:
    """Read ``{"providers": {"<name>": {"api_key": "..."}}}``; unreadable is empty.

    A group- or world-readable file is refused rather than loaded: the file holds
    bearer credentials, and a silent mode downgrade is how they leak into a
    backup or a shared volume. The failure is logged without the key.
    """
    target = path if path is not None else os.getenv(CREDENTIALS_FILE_ENV, "").strip()
    if not target:
        return {}
    location = Path(str(target))
    try:
        mode = location.stat().st_mode & 0o777
    except OSError:
        logger.debug("model credentials file unavailable path=%s", location.name)
        return {}
    if mode & 0o077:
        logger.warning("model credentials file is group/world accessible; ignored path=%s mode=%o",
                       location.name, mode)
        return {}
    try:
        payload = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("model credentials file unreadable path=%s", location.name)
        return {}
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, dict):
        return {}
    credentials: dict[str, str] = {}
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        key = entry.get("api_key")
        if isinstance(key, str) and key.strip():
            credentials[str(name)] = key.strip()
    return credentials


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class Provider(Protocol):
    """What the registry needs from a backend.

    ``auth()`` is a method rather than a stored value so a stored credential or
    an environment change is picked up on the next call, the same way the rest of
    this codebase resolves effective configuration per call.
    """

    name: str
    api: str

    def auth(self) -> ProviderAuth: ...

    def models(self) -> tuple[ModelSpec, ...]: ...


@dataclass(frozen=True)
class EnvProvider:
    """A provider whose endpoint and key come from configuration.

    ``api_key_env`` is read first, then ``stored_api_key`` from the credential
    file. A request-level explicit key is not accepted here: the caller is the
    Python process that already owns these variables.
    """

    name: str
    base_url: str
    api: str = DEFAULT_MODEL_API
    api_key_env: str = API_KEY_ENV
    stored_api_key: str | None = None
    model_specs: tuple[ModelSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ModelPortError("provider 名称不能为空")
        if self.api not in MODEL_APIS:
            raise UnsupportedModelApiError(f"未知的模型 wire api：{self.api}")

    def auth(self) -> ProviderAuth:
        key = os.getenv(self.api_key_env, "").strip()
        source = "env" if key else "none"
        if not key and self.stored_api_key:
            key, source = self.stored_api_key, "stored"
        return ProviderAuth(base_url=self.base_url.rstrip("/"), api_key=key or None, source=source)

    def models(self) -> tuple[ModelSpec, ...]:
        return self.model_specs


def parse_catalog(payload: Any, *, provider: str = "") -> tuple[ModelSpec, ...]:
    """Parse a declared catalog: ``{"models": [{...ModelSpec...}]}`` or a bare list."""
    raw = payload.get("models") if isinstance(payload, Mapping) else payload
    if not isinstance(raw, (list, tuple)):
        raise ModelPortError("模型目录必须是列表或带 models 字段的对象")
    specs: list[ModelSpec] = []
    for entry in raw:
        spec = ModelSpec.from_dict(entry)
        specs.append(replace(spec, provider=spec.provider or provider))
    return tuple(specs)


def load_catalog(path: str | Path | None = None) -> tuple[ModelSpec, ...]:
    """Declared catalog from ``LAW_REVIEW_MODEL_CATALOG`` (inline) or ``_FILE``.

    Absent configuration is the normal state: every model is then undeclared and
    ``resolve(..., allow_undeclared=True)`` still calls it, with ``declared=False``
    recorded in provenance. An unusable catalog is a hard error instead — a
    deployment that believes it declared capabilities must not silently run
    without them.
    """
    inline = os.getenv(CATALOG_ENV, "").strip()
    file_path = path if path is not None else os.getenv(CATALOG_FILE_ENV, "").strip()
    if not inline and not file_path:
        return ()
    try:
        payload = json.loads(inline) if inline else json.loads(Path(str(file_path)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelPortError(f"模型目录不可用（{type(exc).__name__}）") from exc
    return parse_catalog(payload)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    """One call target: the spec, the resolved auth and the provider behind it."""

    spec: ModelSpec
    auth: ProviderAuth
    declared: bool

    @property
    def endpoint(self) -> str:
        return self.auth.base_url

    @property
    def api(self) -> str:
        return self.spec.api

    def provenance(self) -> dict[str, Any]:
        """Identity of the target for an audit record; never the credential."""
        return {
            "model": self.spec.id,
            "api": self.spec.api,
            "provider": self.spec.provider,
            "declared": self.declared,
            "context_window": self.spec.context_window,
            "auth_source": self.auth.source,
        }


class Models:
    """The model catalog: registration, lookup and per-call resolution."""

    def __init__(self, providers: Sequence[Provider] = ()) -> None:
        self._providers: dict[str, Provider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: Provider) -> None:
        """Add or replace a provider by name; last registration wins."""
        if not provider.name.strip():
            raise ModelPortError("provider 名称不能为空")
        self._providers[provider.name] = provider

    def providers(self) -> tuple[Provider, ...]:
        return tuple(self._providers.values())

    def get_model(self, model_id: str) -> ModelSpec | None:
        """The declared spec for ``model_id``, or ``None`` when undeclared."""
        wanted = (model_id or "").strip()
        if not wanted:
            return None
        for provider in self._providers.values():
            for spec in provider.models():
                if spec.id == wanted:
                    return spec
        return None

    def get_available(self) -> tuple[ModelSpec, ...]:
        """Every declared model, ordered by provider then id for stable output."""
        specs = [spec for provider in self._providers.values() for spec in provider.models()]
        return tuple(sorted(specs, key=lambda spec: (spec.provider, spec.id)))

    def resolve(
        self,
        model_id: str,
        *,
        base_url: str | None = None,
        api: str | None = None,
        provider: str | None = None,
        allow_undeclared: bool = False,
        price_column_for: Callable[[str], str | None] | None = None,
    ) -> ResolvedModel:
        """Resolve one call target.

        Precedence is explicit argument > declared spec > provider default, so a
        deployment can override the endpoint or protocol for one call without
        editing the catalog. An api that contradicts the declared spec is used
        but logged: it is a real reconfiguration, not a typo to swallow silently.
        """
        wanted = (model_id or "").strip()
        if not wanted:
            raise UnknownModelError("模型 ID 为空")
        declared_spec = self.get_model(wanted)
        if declared_spec is None and not allow_undeclared:
            # Checked before endpoint resolution: "this model is not in the
            # catalog" is the actionable error, and reporting a missing endpoint
            # first would hide a typo behind a deployment complaint.
            raise UnknownModelError(f"未声明的模型：{wanted}")
        spec = declared_spec
        provider_name = (provider or (spec.provider if spec else "")).strip()
        backend = self._providers.get(provider_name) if provider_name else None
        if backend is None and not provider_name and len(self._providers) == 1:
            # A single-provider registry is the default target: an undeclared
            # model must still resolve that provider's credentials and identity
            # instead of being attributed to an invented backend.
            backend = next(iter(self._providers.values()))
            provider_name = backend.name
        endpoint = (base_url or "").strip().rstrip("/")
        if not endpoint:
            if backend is None:
                raise MissingEndpointError(f"模型 {wanted} 没有可用端点：未注册 provider，也未提供 base_url")
            endpoint = backend.auth().base_url
            if not endpoint:
                raise MissingEndpointError(f"模型 {wanted} 的 provider 未配置 base_url")
        if backend is None and base_url:
            # An explicit endpoint for an unregistered name is still one provider;
            # it keeps the caller's key resolution rather than inventing a key.
            backend = EnvProvider(name=provider_name or "override", base_url=endpoint,
                                  api=(spec.api if spec else (api or DEFAULT_MODEL_API)))
        if backend is None:
            raise MissingEndpointError(f"模型 {wanted} 没有可用 provider")
        resolved_api = (api or (spec.api if spec else "") or backend.api).strip() or DEFAULT_MODEL_API
        if resolved_api not in MODEL_APIS:
            raise UnsupportedModelApiError(f"未知的模型 wire api：{resolved_api}")
        if spec is not None and api and spec.api != api:
            logger.info("model api overridden model=%s declared=%s requested=%s", wanted, spec.api, api)
        if spec is None:
            spec = ModelSpec(id=wanted, api=resolved_api, provider=backend.name)
        elif spec.api != resolved_api:
            spec = replace(spec, api=resolved_api)
        if spec.price_column is None and price_column_for is not None:
            spec = replace(spec, price_column=price_column_for(spec.id))
        auth = backend.auth()
        return ResolvedModel(
            spec=spec,
            auth=ProviderAuth(base_url=endpoint, api_key=auth.api_key, source=auth.source),
            declared=declared_spec is not None,
        )


def configured_api() -> str:
    """The deployment default wire api; an unknown value fails closed."""
    value = os.getenv(API_ENV, DEFAULT_MODEL_API).strip() or DEFAULT_MODEL_API
    if value not in MODEL_APIS:
        raise UnsupportedModelApiError(f"{API_ENV} 只能是 {sorted(MODEL_APIS)} 之一，收到 {value!r}")
    return value


def default_models() -> Models:
    """The registry for this deployment: one provider from the effective config.

    Built per call because the effective base URL/model is resolved per call (the
    workspace UI can change it without a restart). The import of ``app.config`` is
    local so this module stays importable without the app.
    """
    from .config import LLMConfig

    llm = LLMConfig.load()
    api = configured_api()
    credentials = load_credentials()
    return Models([EnvProvider(
        name="local",
        base_url=llm.base_url,
        api=api,
        stored_api_key=credentials.get("local"),
        model_specs=load_catalog(),
    )])


# ---------------------------------------------------------------------------
# Wire format: request, response, stream frames
# ---------------------------------------------------------------------------


def request_path(api: str) -> str:
    try:
        return API_PATH[api]
    except KeyError as exc:
        raise UnsupportedModelApiError(f"未知的模型 wire api：{api}") from exc


def end_frame_types(api: str) -> frozenset[str]:
    """Frame types that end the stream for an api without a ``[DONE]`` marker."""
    return API_END_FRAME_TYPES.get(api, frozenset())


def build_chat_request(
    *,
    api: str,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    stream: bool,
    thinking: bool = False,
) -> dict[str, Any]:
    """One request body in the wire format the provider speaks.

    ``thinking`` only shapes the OpenAI-compatible body: those servers take the
    switch through a chat-template argument, while ``anthropic-messages`` simply
    omits the ``thinking`` field when reasoning is not requested. A disable flag
    that the server ignores is still sent, because a wrapped template can prepend
    a reasoning block on its own and the response path strips it either way.
    """
    if api == MODEL_API_OPENAI_COMPLETIONS:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body
    if api == MODEL_API_ANTHROPIC_MESSAGES:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if system:
            body["system"] = system
        if thinking:
            body["thinking"] = {"type": "enabled"}
        return body
    raise UnsupportedModelApiError(f"未知的模型 wire api：{api}")


def parse_completion(api: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Answer text and usage from one non-streaming response body."""
    if api == MODEL_API_ANTHROPIC_MESSAGES:
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise ModelPortError("anthropic-messages 响应缺少 content 列表")
        text = "".join(
            block.get("text", "") for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        usage = payload.get("usage")
        return text, usage if isinstance(usage, dict) else None
    if api == MODEL_API_OPENAI_COMPLETIONS:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ModelPortError("openai-completions 响应缺少 choices")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise ModelPortError("openai-completions 响应缺少 message")
        content = message.get("content")
        usage = payload.get("usage")
        return (content if isinstance(content, str) else ""), (usage if isinstance(usage, dict) else None)
    raise UnsupportedModelApiError(f"未知的模型 wire api：{api}")


def parse_stream_frame(api: str, frame: Mapping[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Text delta and usage from one decoded SSE frame.

    The caller merges usage objects: ``anthropic-messages`` reports input tokens
    in ``message_start`` and output tokens in ``message_delta``, so replacing the
    whole object would lose one half of the count.
    """
    if api == MODEL_API_ANTHROPIC_MESSAGES:
        kind = frame.get("type")
        if kind == "content_block_delta":
            delta = frame.get("delta")
            if isinstance(delta, Mapping) and delta.get("type") == "text_delta":
                text = delta.get("text")
                return (text if isinstance(text, str) else None), None
            return None, None
        if kind == "message_start":
            message = frame.get("message")
            usage = message.get("usage") if isinstance(message, Mapping) else None
            return None, usage if isinstance(usage, dict) else None
        if kind == "message_delta":
            usage = frame.get("usage")
            return None, usage if isinstance(usage, dict) else None
        return None, None
    if api == MODEL_API_OPENAI_COMPLETIONS:
        usage = frame.get("usage")
        captured = usage if isinstance(usage, dict) else None
        choices = frame.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            return None, captured
        delta = choices[0].get("delta")
        content = delta.get("content") if isinstance(delta, Mapping) else None
        return (content if isinstance(content, str) and content else None), captured
    raise UnsupportedModelApiError(f"未知的模型 wire api：{api}")


# ---------------------------------------------------------------------------
# Reasoning across a model handoff
# ---------------------------------------------------------------------------


def strip_thinking_text(text: str) -> str:
    """Remove ``<think>`` runs, including an unterminated trailing one."""
    return _THINKING_OPEN_TAIL.sub("", _THINKING_PATTERN.sub("", text)).strip()


def drop_thinking_blocks(message: Mapping[str, Any]) -> dict[str, Any]:
    """One message with its reasoning removed, structure preserved.

    A message whose content was *only* reasoning becomes empty; ``handoff_messages``
    drops those turns entirely, because the next model cannot verify a signature
    it did not produce and has nothing left to answer from.
    """
    cleaned = dict(message)
    content = cleaned.get("content")
    if isinstance(content, str):
        cleaned["content"] = strip_thinking_text(content)
        return cleaned
    if isinstance(content, list):
        blocks: list[Any] = []
        for block in content:
            if isinstance(block, Mapping):
                if str(block.get("type") or "") in THINKING_BLOCK_TYPES:
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    blocks.append({**block, "text": strip_thinking_text(block["text"])})
                    continue
            blocks.append(block)
        cleaned["content"] = [block for block in blocks if block != {"type": "text", "text": ""}]
    return cleaned


def handoff_messages(
    messages: Sequence[Mapping[str, Any]], *, source_model: str, target_model: str
) -> list[dict[str, Any]]:
    """Conversation history prepared for ``target_model``.

    Same model keeps the turn byte-for-byte: the signatures are still valid and
    nothing is lost. A different model gets the turns with reasoning removed and
    with reasoning-only turns dropped.
    """
    if source_model.strip() == target_model.strip():
        # Deep-copied, not aliased: a caller that appends to a prepared turn must
        # not reach into the stored transcript through the shared content list.
        return [copy.deepcopy(dict(message)) for message in messages]
    prepared: list[dict[str, Any]] = []
    for message in messages:
        cleaned = drop_thinking_blocks(message)
        if cleaned.get("content") in ("", [], None):
            continue
        prepared.append(cleaned)
    return prepared


# ---------------------------------------------------------------------------
# Scripted provider (the faux backend)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptedReply:
    """One queued scripted response and the usage it reports."""

    text: str
    usage: dict[str, Any] | None = None


class ScriptedProvider:
    """Deterministic in-memory provider for tests and benchmarks.

    Replies are served from a queue, so a test can assert behaviour under a model
    whose output is known instead of hoping a live server cooperates.
    ``tokens_per_second`` paces emission; the pause goes through the injected
    ``sleep`` so a test asserts the pacing without waiting for it.
    """

    def __init__(
        self,
        replies: Sequence[ScriptedReply | str] = (),
        *,
        name: str = "scripted",
        api: str = DEFAULT_MODEL_API,
        base_url: str = "http://scripted.invalid/v1",
        model: str = "scripted-model",
        tokens_per_second: float = 0.0,
        chunk_size: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        supports_tools: bool = False,
    ) -> None:
        if api not in MODEL_APIS:
            raise UnsupportedModelApiError(f"未知的模型 wire api：{api}")
        if chunk_size < 1:
            raise ModelPortError("chunk_size 必须为正整数")
        if tokens_per_second < 0:
            raise ModelPortError("tokens_per_second 不能为负")
        self.name = name
        self.api = api
        self.base_url = base_url
        self.model_name = model
        self.tokens_per_second = tokens_per_second
        self.chunk_size = chunk_size
        self.supports_tools = supports_tools
        self._sleep = sleep
        self.requests: list[dict[str, Any]] = []
        self._replies: list[ScriptedReply] = [
            reply if isinstance(reply, ScriptedReply) else ScriptedReply(reply) for reply in replies
        ]

    def auth(self) -> ProviderAuth:
        return ProviderAuth(base_url=self.base_url, source="none")

    def models(self) -> tuple[ModelSpec, ...]:
        return (ModelSpec(id=self.model_name, api=self.api, provider=self.name,
                          supports_tools=self.supports_tools),)

    @property
    def remaining(self) -> int:
        return len(self._replies)

    def _record(self, request: Mapping[str, Any]) -> None:
        # Copy so a caller mutating its payload cannot rewrite the record.
        self.requests.append(dict(request))

    def _chunks(self, text: str) -> Iterator[str]:
        pause = 1.0 / self.tokens_per_second if self.tokens_per_second > 0 else 0.0
        for start in range(0, len(text), self.chunk_size):
            if pause:
                self._sleep(pause)
            yield text[start:start + self.chunk_size]

    def _next_reply(self) -> ScriptedReply:
        if not self._replies:
            raise ScriptedQueueEmpty("scripted provider 的回复队列已耗尽")
        return self._replies.pop(0)

    def chat(self, request: Mapping[str, Any]) -> ScriptedReply:
        """Serve one reply, draining the paced chunks before returning."""
        self._record(request)
        reply = self._next_reply()
        for _ in self._chunks(reply.text):
            pass
        return reply

    def stream(self, request: Mapping[str, Any]) -> Iterator[str]:
        """Yield one reply in chunks so a streaming caller can be exercised."""
        self._record(request)
        reply = self._next_reply()
        return self._chunks(reply.text)
