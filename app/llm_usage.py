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

from typing import Any, Iterable, Sequence

from .db import now, transaction


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
) -> None:
    """Append one inference call. Never raises into the answer path."""
    input_tokens, output_tokens = token_counts(usage)
    with transaction() as conn:
        conn.execute(
            """INSERT INTO llm_calls(case_id,run_id,conversation_id,kind,model,input_tokens,
                                     output_tokens,ttft_ms,latency_ms,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
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
            ),
        )


def _sum_known(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _mean_known(values: list[int | None]) -> float | None:
    known = [value for value in values if value is not None]
    return round(sum(known) / len(known), 2) if known else None


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or "unknown"), []).append(row)
    return [
        {
            key: name,
            "calls": len(items),
            "input_tokens": _sum_known([item["input_tokens"] for item in items]),
            "output_tokens": _sum_known([item["output_tokens"] for item in items]),
            "latency_p50_ms": percentile([item["latency_ms"] for item in items], 0.5),
            "latency_p95_ms": percentile([item["latency_ms"] for item in items], 0.95),
        }
        for name, items in sorted(grouped.items())
    ]


def usage_summary(case_id: int) -> dict[str, Any]:
    """Case-scoped cost and latency summary.

    ``input_tokens``/``output_tokens`` and the TTFT aggregates are ``None`` when
    no call reported them. That is deliberately distinct from a measured zero.
    """
    with transaction() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT kind, model, input_tokens, output_tokens, ttft_ms, latency_ms, status
                   FROM llm_calls WHERE case_id=? ORDER BY id""",
                (case_id,),
            ).fetchall()
        ]
    calls = len(rows)
    if not calls:
        return {
            "calls": 0,
            "failed": 0,
            "input_tokens": None,
            "output_tokens": None,
            "ttft_avg_ms": None,
            "ttft_p50_ms": None,
            "ttft_p95_ms": None,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "by_kind": [],
            "by_model": [],
        }
    ttft_values = [row["ttft_ms"] for row in rows]
    return {
        "calls": calls,
        "failed": sum(1 for row in rows if row["status"] != "ok"),
        "input_tokens": _sum_known([row["input_tokens"] for row in rows]),
        "output_tokens": _sum_known([row["output_tokens"] for row in rows]),
        "ttft_avg_ms": _mean_known(ttft_values),
        "ttft_p50_ms": percentile([value for value in ttft_values if value is not None], 0.5),
        "ttft_p95_ms": percentile([value for value in ttft_values if value is not None], 0.95),
        "latency_p50_ms": percentile([row["latency_ms"] for row in rows], 0.5),
        "latency_p95_ms": percentile([row["latency_ms"] for row in rows], 0.95),
        "by_kind": _group(rows, "kind"),
        "by_model": _group(rows, "model"),
    }
