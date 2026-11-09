"""Optional second model target for answers that fail the citation contract.

Routing is deliberately narrow: the primary model is always tried first, and the
fallback runs at most once, only when the primary answer was produced but
rejected by ``validate_review_answer``. A primary *transport* failure follows the
existing failure path (rule summary plus diagnostic) instead of a second model;
that keeps the two failure classes distinguishable. It is not a general
multi-model scheduler, and it never weakens the contract — the fallback answer
has to pass the same checks with the same case scope.

The target carries its own wire api: a fallback commonly runs on a different
backend than the primary, and assuming the primary's protocol would send an
OpenAI-shaped body to an Anthropic-shaped endpoint (or the reverse) and fail with
an opaque 4xx instead of an answer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.llm.model_ports import DEFAULT_MODEL_API, MODEL_APIS, configured_api
from app.review.services import assert_model_endpoint_allowed


@dataclass(frozen=True)
class FallbackTarget:
    base_url: str
    model: str
    api: str = DEFAULT_MODEL_API


class FallbackConfigError(ValueError):
    """The fallback variables are set but not usable."""


def fallback_enabled() -> bool:
    return os.getenv("LAW_REVIEW_LLM_FALLBACK", "off").strip().lower() in {"1", "on", "true", "yes"}


def fallback_api() -> str:
    """Wire api for the fallback endpoint; defaults to the deployment default."""
    value = os.getenv("LAW_REVIEW_LLM_FALLBACK_API", "").strip()
    if not value:
        return configured_api()
    if value not in MODEL_APIS:
        raise FallbackConfigError(
            f"LAW_REVIEW_LLM_FALLBACK_API 只能是 {sorted(MODEL_APIS)} 之一，收到 {value!r}"
        )
    return value


def fallback_target() -> FallbackTarget | None:
    """Return the configured fallback or ``None`` when routing is off.

    A half-configured endpoint raises instead of silently degrading to the
    primary: a deployment that believes it has a fallback must not discover
    otherwise during an incident.
    """
    if not fallback_enabled():
        return None
    url = os.getenv("LAW_REVIEW_LLM_FALLBACK_URL", "").strip().rstrip("/")
    model = os.getenv("LAW_REVIEW_LLM_FALLBACK_MODEL", "").strip()
    if not url or not model:
        raise FallbackConfigError("LAW_REVIEW_LLM_FALLBACK 已开启，但 URL 或 MODEL 为空")
    try:
        assert_model_endpoint_allowed(url)
    except ValueError as exc:
        raise FallbackConfigError(f"回退端点不被允许：{exc}") from exc
    return FallbackTarget(base_url=url, model=model, api=fallback_api())
