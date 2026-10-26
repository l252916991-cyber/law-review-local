"""Structured business audit entries for collaborative edits.

The audit trail answers "who changed which field, from what to what". Values are
bounded so a long quote or fact cannot turn one audit row into a copy of the
record, and no request bodies or secrets are written.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import now

AUDIT_VALUE_LIMIT = 200


def bounded_value(value: Any) -> Any:
    """Keep scalar values intact and clamp free text to a reviewable length."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(rendered) <= AUDIT_VALUE_LIMIT:
        return rendered
    return rendered[:AUDIT_VALUE_LIMIT] + "…"


def record_field_changes(
    conn: sqlite3.Connection,
    *,
    case_id: int | None,
    action: str,
    target: str,
    actor: str,
    before: dict[str, Any],
    after: dict[str, Any],
    versions: tuple[int, int] | None = None,
) -> list[str]:
    """Insert one audit row when at least one field really changed.

    ``before`` and ``after`` must describe the same keys; unchanged keys are
    dropped. Returns the changed field names so callers can report them.
    """
    changed = {
        key: {"before": bounded_value(before.get(key)), "after": bounded_value(after.get(key))}
        for key in after
        if before.get(key) != after.get(key)
    }
    if not changed:
        return []
    detail: dict[str, Any] = {"target": target, "actor": actor, "fields": changed}
    if versions is not None:
        detail["version"] = {"from": versions[0], "to": versions[1]}
    conn.execute(
        "INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,?,?,?)",
        (case_id, action, json.dumps(detail, ensure_ascii=False), now()),
    )
    return list(changed)
