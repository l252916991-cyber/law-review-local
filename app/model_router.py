"""Optional second model target for answers that fail the citation contract.

Routing is deliberately narrow: the primary model is always tried first, and
the fallback only runs when the primary answer is unavailable or rejected by
``validate_review_answer``. It is not a general multi-model scheduler, and it
never weakens the contract — the fallback answer has to pass the same checks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .services import assert_model_endpoint_allowed


@dataclass(frozen=True)
class FallbackTarget:
    base_url: str
    model: str


class FallbackConfigError(ValueError):
    """The fallback variables are set but not usable."""


def fallback_enabled() -> bool:
    return os.getenv("LAW_REVIEW_LLM_FALLBACK", "off").strip().lower() in {"1", "on", "true", "yes"}


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
    return FallbackTarget(base_url=url, model=model)
