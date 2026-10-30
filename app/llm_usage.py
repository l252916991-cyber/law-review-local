"""Per-call model accounting: tokens, latency and first-token delay.

A cost report is an observation, not an estimate. When the upstream server omits
``usage`` the token columns stay ``NULL`` and the average is reported as unknown;
recording zero instead would silently drag every average toward zero and make a
degraded backend look cheap.

Percentiles use linear interpolation rather than a bucket table. With a few
hundred calls a bucket table hides exactly the slow tail the number is meant to
expose.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Sequence

from .db import now, transaction
from .pricing import DISPLAY_QUANTUM, call_cost, format_money, load_pricing


PROMPT_KEYS: tuple[str, ...] = ("prompt_tokens", "input_tokens")
COMPLETION_KEYS: tuple[str, ...] = ("completion_tokens", "output_tokens")


def _first_int(mapping: Any, keys: Sequence[str]) -> int | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        # ``bool`` is an ``int`` subclass; a flag must never become a token count.
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


def token_counts(usage: Any) -> tuple[int | None, int | None]:
    """Read both OpenAI and local naming conventions; unknown stays ``None``."""
    if not isinstance(usage, dict):
        return None, None
    return _first_int(usage, PROMPT_KEYS), _first_int(usage, COMPLETION_KEYS)


def percentile(values: Iterable[float | int], ratio: float) -> float | None:
    """Linear-interpolation percentile; ``None`` for an empty sample."""
    if not 0 <= ratio <= 1:
        raise ValueError("ratio must be within [0, 1]")
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 2)


def record_llm_call(
    *,
    case_id: int,
    kind: str,
    model: str,
    usage: Any,
    latency_ms: int,
    ttft_ms: int | None = None,
    status: str = "ok",
    run_id: int | None = None,
    conversation_id: int | None = None,
    turn_key: str | None = None,
    price_column: str | None = None,
) -> None:
    """Append one inference call with the price snapshot used to price it.

    Never raises into the answer path: a missing or unusable pricing
    configuration still records the call, with a ``NULL`` snapshot (cost unknown).
    ``price_column`` lets the caller that already resolved the model's snapshot
    column pass it through; when absent the column is looked up by model id.
    """
    input_tokens, output_tokens = token_counts(usage)
    snapshot, error = load_pricing()
    if error:
        snapshot = None
    pricing_column = price_column
    if pricing_column is None and snapshot is not None:
        pricing_column = snapshot.column_for(model)
    # Agent runs already carry a run id; other turns use the client request id or
    # a per-turn key, so both can be grouped as one logical question.
    if turn_key is None and run_id is not None:
        turn_key = f"run:{run_id}"
    with transaction() as conn:
        conn.execute(
            """INSERT INTO llm_calls(case_id,run_id,conversation_id,kind,model,input_tokens,
                                     output_tokens,ttft_ms,latency_ms,status,created_at,
                                     pricing_json,turn_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                run_id,
                conversation_id,
                kind,
                model or "",
                input_tokens,
                output_tokens,
                ttft_ms,
                max(0, int(latency_ms)),
                status if status in {"ok", "failed"} else "failed",
                now(),
                pricing_column,
                turn_key,
            ),
        )


def _sum_known(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _mean_known(values: list[int | None]) -> float | None:
    known = [value for value in values if value is not None]
    return round(sum(known) / len(known), 2) if known else None


def _known_calls(values: list[int | None]) -> int:
    """How many samples carried a measurement; the denominator for coverage.

    A caller that shows only the sum cannot tell "3 calls, 30 tokens" from
    "1 of 3 calls reported 30 tokens". Both numbers are needed before the sum
    may be read as anything other than a partial observation.
    """
    return sum(1 for value in values if value is not None)


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or "unknown"), []).append(row)
    return [
        {
            key: name,
            "calls": len(items),
            "failed": sum(1 for item in items if item["status"] != "ok"),
            "input_tokens": _sum_known([item["input_tokens"] for item in items]),
            "output_tokens": _sum_known([item["output_tokens"] for item in items]),
            "input_tokens_known_calls": _known_calls([item["input_tokens"] for item in items]),
            "output_tokens_known_calls": _known_calls([item["output_tokens"] for item in items]),
            "ttft_p50_ms": percentile([item["ttft_ms"] for item in items if item["ttft_ms"] is not None], 0.5),
            "ttft_p95_ms": percentile([item["ttft_ms"] for item in items if item["ttft_ms"] is not None], 0.95),
            "ttft_known_calls": _known_calls([item["ttft_ms"] for item in items]),
            "latency_p50_ms": percentile([item["latency_ms"] for item in items], 0.5),
            "latency_p95_ms": percentile([item["latency_ms"] for item in items], 0.95),
        }
        for name, items in sorted(grouped.items())
    ]


def _cost_of(row: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """Per-call cost and its currency; ``(None, None)`` when it cannot be priced."""
    column = row.get("pricing_json")
    if not column:
        return None, None
    import json

    currency = json.loads(column).get("currency")
    return call_cost(column, row.get("input_tokens"), row.get("output_tokens")), currency


def _money(amount: Decimal | None, currency: str | None) -> str | None:
    return format_money(amount, currency)


def cost_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Call-level cost with an explicit unknown bucket.

    ``total`` is only present when every call could be priced; otherwise the
    known part is reported as a floor with its coverage. Costs are never summed
    across currencies — that would be an invented exchange rate.
    A call reporting only input or output tokens contributes its known cost
    to the floor, but remains a missing-usage call until both sides are known.
    """
    snapshot, error = load_pricing()
    priced: list[tuple[Decimal, str]] = []
    no_price = 0
    no_usage = 0
    partial_usage = 0
    currencies: set[str] = set()
    versions: set[str] = set()
    sources: set[str] = set()
    units: set[int] = set()
    for row in rows:
        cost, currency = _cost_of(row)
        if row.get("pricing_json"):
            import json

            payload = json.loads(row["pricing_json"])
            versions.add(str(payload.get("version") or ""))
            sources.add(str(payload.get("source") or ""))
            units.add(int(payload.get("unit_tokens") or 0))
        if cost is None:
            if not row.get("pricing_json"):
                no_price += 1
            else:
                no_usage += 1
            continue
        assert currency is not None
        if row.get("input_tokens") is None or row.get("output_tokens") is None:
            partial_usage += 1
        currencies.add(currency)
        priced.append((cost, currency))
    calls = len(rows)
    known = len(priced)
    single_currency = next(iter(currencies)) if len(currencies) == 1 else None
    # Summing different currencies would invent an exchange rate, so a mixed set
    # reports no subtotal at all rather than a number with a missing unit.
    partial_total = (
        sum((amount for amount, _ in priced), Decimal(0))
        if priced and single_currency is not None else None
    )
    complete = bool(calls) and known == calls and partial_usage == 0 and single_currency is not None
    return {
        "configured": snapshot is not None or bool(known) or no_price < calls,
        "config_error": error,
        "currency": single_currency,
        "mixed_currency": len(currencies) > 1,
        # The panel describes recorded calls, so the unit comes from the stored
        # snapshots rather than the current configuration.
        "unit_tokens": next(iter(units)) if len(units) == 1 else
        (snapshot.unit_tokens if snapshot is not None and not units else None),
        "version": next(iter(versions)) if len(versions) == 1 else None,
        "versions": sorted(value for value in versions if value),
        "source": next(iter(sources)) if len(sources) == 1 else None,
        "calls": calls,
        "known_calls": known - partial_usage,
        "no_price_calls": no_price,
        "usage_missing_calls": no_usage + partial_usage,
        "partial_total": _money(partial_total, single_currency),
        "total": _money(partial_total, single_currency) if complete else None,
    }


def _turn_rows(rows: list[dict[str, Any]], limit: int = 8) -> tuple[list[dict[str, Any]], int]:
    """Group calls by the logical question they belong to.

    Unattributed calls are reported separately instead of being hidden in a
    turn: the sum of turn costs is allowed to be smaller than the call total.
    """
    grouped: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get("turn_key"), []).append(row)
    turns: list[dict[str, Any]] = []
    for key, items in grouped.items():
        costs = [_cost_of(item) for item in items]
        currencies = {currency for _, currency in costs if currency is not None}
        known = [cost for cost, _ in costs if cost is not None]
        complete_calls = sum(
            cost is not None and item.get("input_tokens") is not None and item.get("output_tokens") is not None
            for item, (cost, _) in zip(items, costs)
        )
        single_currency = next(iter(currencies)) if len(currencies) == 1 else None
        total = sum(known, Decimal(0)) if known else None
        turns.append({
            "turn_key": key,
            "calls": len(items),
            "kinds": sorted({str(item.get("kind") or "") for item in items}),
            "models": sorted({str(item.get("model") or "") for item in items}),
            "input_tokens": _sum_known([item.get("input_tokens") for item in items]),
            "output_tokens": _sum_known([item.get("output_tokens") for item in items]),
            "known_calls": complete_calls,
            "unknown_calls": len(items) - complete_calls,
            "mixed_currency": len(currencies) > 1,
            "cost": _money(total, single_currency) if complete_calls == len(items) and single_currency else None,
            "partial_cost": _money(total, single_currency) if known and single_currency else None,
        })
    # Newest turn first: call rows arrive in order, so the last item is the latest.
    turns.sort(key=lambda item: item["turn_key"] or "", reverse=True)
    return turns[:limit], len(turns)


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate call rows; shared by the case, run and turn scopes."""
    calls = len(rows)
    if not calls:
        snapshot, error = load_pricing()
        return {
            "calls": 0,
            "failed": 0,
            "input_tokens": None,
            "output_tokens": None,
            "input_tokens_known_calls": 0,
            "output_tokens_known_calls": 0,
            "ttft_avg_ms": None,
            "ttft_p50_ms": None,
            "ttft_p95_ms": None,
            "ttft_known_calls": 0,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "by_kind": [],
            "by_model": [],
            "cost": {
                "configured": snapshot is not None,
                "config_error": error,
                "currency": None, "mixed_currency": False, "unit_tokens": None,
                "version": None, "versions": [], "source": None,
                "calls": 0, "known_calls": 0, "no_price_calls": 0,
                "usage_missing_calls": 0, "partial_total": None, "total": None,
            },
            "turn_count": 0,
            "turns": [],
        }
    input_values = [row["input_tokens"] for row in rows]
    output_values = [row["output_tokens"] for row in rows]
    ttft_values = [row["ttft_ms"] for row in rows]
    turns, turn_count = _turn_rows(rows)
    return {
        "calls": calls,
        "failed": sum(1 for row in rows if row["status"] != "ok"),
        "input_tokens": _sum_known(input_values),
        "output_tokens": _sum_known(output_values),
        "input_tokens_known_calls": _known_calls(input_values),
        "output_tokens_known_calls": _known_calls(output_values),
        "ttft_avg_ms": _mean_known(ttft_values),
        "ttft_p50_ms": percentile([value for value in ttft_values if value is not None], 0.5),
        "ttft_p95_ms": percentile([value for value in ttft_values if value is not None], 0.95),
        "ttft_known_calls": _known_calls(ttft_values),
        "latency_p50_ms": percentile([row["latency_ms"] for row in rows], 0.5),
        "latency_p95_ms": percentile([row["latency_ms"] for row in rows], 0.95),
        "by_kind": _group(rows, "kind"),
        "by_model": _group(rows, "model"),
        "cost": cost_summary(rows),
        "turn_count": turn_count,
        "turns": turns,
    }


_CALL_COLUMNS = """kind, model, input_tokens, output_tokens, ttft_ms, latency_ms, status,
                   pricing_json, turn_key"""


def usage_summary(case_id: int) -> dict[str, Any]:
    """Case-scoped cost and latency summary.

    ``input_tokens``/``output_tokens`` and the TTFT aggregates are ``None`` when
    no call reported them. That is deliberately distinct from a measured zero.
    The ``*_known_calls`` counters are the sample size behind each aggregate, so
    a partial sum is never presented as the complete total. Latency is measured
    per model call; the summary makes no claim about whole-turn latency.
    """
    with transaction() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT {_CALL_COLUMNS} FROM llm_calls WHERE case_id=? ORDER BY id",
                (case_id,),
            ).fetchall()
        ]
    return _summarize(rows)


def usage_for_run(run_id: int) -> dict[str, Any]:
    """Usage of one agent run, including its tool rounds and fallback call."""
    with transaction() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT {_CALL_COLUMNS} FROM llm_calls WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        ]
    return _summarize(rows)
