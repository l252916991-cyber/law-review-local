"""Token pricing snapshots for estimated model cost.

A price is configuration, not an observation. Every call row stores the snapshot
it was priced with, so a later rate change cannot rewrite history. Nothing here is
a bill: the numbers are token counts times a configured unit price, and a call
whose tokens or price are unknown contributes no money at all.

Configuration comes from ``LAW_REVIEW_PRICING_JSON`` (inline JSON) or
``LAW_REVIEW_PRICING_FILE`` (path):

```json
{"version": "2026-10-25.1", "currency": "CNY", "unit_tokens": 1000000,
 "source": "vendor price sheet 2026-10",
 "models": {"model-a": {"input": "2.0", "output": "8.0"}}}
```

Missing configuration is a valid state: costs render as "未配置成本" instead of
¥0. Invalid configuration fails closed (no cost) and reports why.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Internal precision is finer than anything a token price sheet needs; display
# precision is fixed so two runs of the same data render identically.
PRICE_QUANTUM = Decimal("0.00000001")
DISPLAY_QUANTUM = Decimal("0.0001")


class PricingConfigError(ValueError):
    """The pricing configuration exists but cannot be used."""


@dataclass(frozen=True)
class ModelPrice:
    input_per_unit: Decimal
    output_per_unit: Decimal


@dataclass(frozen=True)
class PriceSnapshot:
    version: str
    currency: str
    unit_tokens: int
    source: str
    models: dict[str, ModelPrice]

    def for_model(self, model: str) -> ModelPrice | None:
        return self.models.get(model or "")

    def column_for(self, model: str) -> str | None:
        """The immutable per-call snapshot, ``None`` when this model has no price."""
        price = self.for_model(model)
        if price is None:
            return None
        return json.dumps({
            "version": self.version,
            "currency": self.currency,
            "unit_tokens": self.unit_tokens,
            "source": self.source,
            "input": str(price.input_per_unit),
            "output": str(price.output_per_unit),
        }, ensure_ascii=False, sort_keys=True)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PricingConfigError(f"{field} 不是合法数字") from exc
    if not parsed.is_finite() or parsed < 0:
        raise PricingConfigError(f"{field} 必须是非负有限数")
    return parsed


def parse_snapshot(payload: Any) -> PriceSnapshot:
    if not isinstance(payload, dict):
        raise PricingConfigError("定价配置必须是 JSON 对象")
    version = str(payload.get("version") or "").strip()
    currency = str(payload.get("currency") or "").strip().upper()
    source = str(payload.get("source") or "").strip()
    unit_tokens = payload.get("unit_tokens")
    if not version or not currency or not source:
        raise PricingConfigError("定价配置需要 version、currency 与 source")
    if type(unit_tokens) is not int or unit_tokens < 1:
        raise PricingConfigError("unit_tokens 必须是正整数")
    models: dict[str, ModelPrice] = {}
    raw_models = payload.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise PricingConfigError("定价配置需要至少一个模型单价")
    for name, price in raw_models.items():
        if not isinstance(price, dict):
            raise PricingConfigError(f"模型 {name} 的单价必须是对象")
        models[str(name)] = ModelPrice(
            input_per_unit=_decimal(price.get("input"), f"{name}.input"),
            output_per_unit=_decimal(price.get("output"), f"{name}.output"),
        )
    return PriceSnapshot(version=version, currency=currency, unit_tokens=unit_tokens,
                         source=source, models=models)


def load_pricing() -> tuple[PriceSnapshot | None, str | None]:
    """Return ``(snapshot, error)``; both empty means pricing is simply unset."""
    inline = os.getenv("LAW_REVIEW_PRICING_JSON", "").strip()
    path = os.getenv("LAW_REVIEW_PRICING_FILE", "").strip()
    if not inline and not path:
        return None, None
    try:
        raw: Any
        if inline:
            raw = json.loads(inline)
        else:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return parse_snapshot(raw), None
    except (OSError, json.JSONDecodeError, PricingConfigError) as exc:
        logger.warning("pricing_config_unusable error_type=%s", type(exc).__name__)
        return None, f"定价配置不可用（{type(exc).__name__}）"


def call_cost(column: str | None, input_tokens: int | None, output_tokens: int | None) -> Decimal | None:
    """Cost of one call, or ``None`` when any factor is unknown.

    A failed call with no reported usage therefore costs *nothing known*, not
    zero: the caller must keep it in the unknown bucket.
    """
    if not column:
        return None
    if input_tokens is None and output_tokens is None:
        return None
    snapshot = json.loads(column)
    unit = Decimal(int(snapshot["unit_tokens"]))
    amount = Decimal(0)
    if input_tokens is not None:
        amount += Decimal(int(input_tokens)) / unit * Decimal(str(snapshot["input"]))
    if output_tokens is not None:
        amount += Decimal(int(output_tokens)) / unit * Decimal(str(snapshot["output"]))
    return amount.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def format_money(amount: Decimal | None, currency: str | None) -> str | None:
    """Fixed display precision; ``None`` stays ``None`` (never "¥0")."""
    if amount is None:
        return None
    return f"{amount.quantize(DISPLAY_QUANTUM, rounding=ROUND_HALF_UP)} {currency or ''}".strip()
