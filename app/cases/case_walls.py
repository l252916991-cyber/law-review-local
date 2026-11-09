"""Information walls (信息隔离墙): a walled case is visible only to the people inside it.

A firm that acts for both sides of related matters, or that hires a lawyer from
the other side, must keep those people away from the case. Ordinary case access
already limits members to their assigned cases; a wall goes further:

* only the listed insiders can open the case -- administrators included;
* everyone else gets the same 404 as for a case that does not exist, and the
  case disappears from every list, count and audit feed they can read;
* a compliance officer outside the wall can take a break-glass grant: it needs
  a written reason, lasts ``BREAK_GLASS_SECONDS``, and lands in the audit log
  and the security trail.

Enforcement has two independent layers. The application checks every case
path and filters every cross-case list (both backends). On PostgreSQL,
row-level security policies (``app/storage/schema_postgres.sql``) hide the rows inside
the database as well, so an endpoint that forgets the check still cannot read a
walled page. Walls need distinct identities, so they cannot be set in local
single-user mode.
"""
from __future__ import annotations

import time
from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.storage.db import connect, now, record_security_event, system_access, transaction

BREAK_GLASS_SECONDS = 30 * 60
NOT_FOUND = "案件不存在或无访问权限"

router = APIRouter()


def _principal():
    from app.core.security import _principal as principal_var

    return principal_var.get()


def hidden_case_ids() -> frozenset[int]:
    """Walled cases the current request's principal may not see.

    Outside a request (startup, workers, scripts) nothing is hidden: the wall
    governs people, and maintenance acts for the system.
    """
    principal = _principal()
    if principal is None:
        return frozenset()
    with system_access(), closing(connect()) as conn:
        rows = conn.execute(
            """SELECT w.case_id FROM case_walls w
               WHERE NOT EXISTS (SELECT 1 FROM case_wall_insiders i
                                 WHERE i.case_id = w.case_id AND i.principal_name = ?)
                 AND NOT EXISTS (SELECT 1 FROM case_wall_grants g
                                 WHERE g.case_id = w.case_id AND g.principal_name = ? AND g.expires_at > ?)""",
            (principal.name, principal.name, int(time.time())),
        ).fetchall()
    return frozenset(int(row[0]) for row in rows)


def exclusion_clause(column: str, hidden: frozenset[int]) -> tuple[str, tuple[int, ...]]:
    """``AND column NOT IN (...)`` for SQL that lists across cases (empty when nothing is hidden)."""
    if not hidden:
        return "", ()
    return f" AND ({column} IS NULL OR {column} NOT IN ({','.join('?' for _ in hidden)}))", tuple(sorted(hidden))


def _require_admin() -> Any:
    from app.core.security import current_principal, require_permission

    require_permission("manage")
    return current_principal()


def _wall(conn, case_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM case_walls WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    insiders = [r[0] for r in conn.execute(
        "SELECT principal_name FROM case_wall_insiders WHERE case_id = ? ORDER BY principal_name", (case_id,))]
    grants = [dict(zip(("principal", "reason", "granted_at", "expires_at"), r, strict=True)) for r in conn.execute(
        "SELECT principal_name, reason, granted_at, expires_at FROM case_wall_grants WHERE case_id = ? ORDER BY id DESC",
        (case_id,))]
    # Titles stay out: whoever manages the wall may be outside it.
    return {"case_id": case_id, "reason": row["reason"], "created_by": row["created_by"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "insiders": insiders, "grants": grants}


def _audit(conn, case_id: int, action: str, detail: str) -> None:
    conn.execute("INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                 (case_id, action, detail, now()))


class WallIn(BaseModel):
    reason: str = Field(min_length=4, max_length=500)
    insiders: list[str] = Field(min_length=1, max_length=100)


class BreakGlassIn(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


@router.get("/api/case-walls")
def list_walls() -> list[dict[str, Any]]:
    _require_admin()
    with system_access(), closing(connect()) as conn:
        ids = [row[0] for row in conn.execute("SELECT case_id FROM case_walls ORDER BY case_id")]
        return [wall for case_id in ids if (wall := _wall(conn, case_id)) is not None]


@router.put("/api/case-walls/{case_id}")
def set_wall(case_id: int, body: WallIn) -> dict[str, Any]:
    principal = _require_admin()
    from app.core.security import auth_mode

    if auth_mode() == "local":
        raise HTTPException(409, "本机单人模式只有一个身份，隔离墙无法区分墙内外；请在 token 或 OIDC 模式下设置")
    insiders = sorted({name.strip() for name in body.insiders if name.strip()})
    if not insiders or any(len(name) > 100 for name in insiders):
        raise HTTPException(422, "墙内人员名单无效")
    stamp = now()
    with system_access(), transaction() as conn:
        if conn.execute("SELECT 1 FROM cases WHERE id = ?", (case_id,)).fetchone() is None:
            raise HTTPException(404, NOT_FOUND)
        existed = conn.execute("SELECT 1 FROM case_walls WHERE case_id = ?", (case_id,)).fetchone() is not None
        conn.execute(
            "INSERT INTO case_walls(case_id, reason, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(case_id) DO UPDATE SET reason = excluded.reason, updated_at = excluded.updated_at",
            (case_id, body.reason.strip(), principal.name, stamp, stamp),
        )
        conn.execute("DELETE FROM case_wall_insiders WHERE case_id = ?", (case_id,))
        for name in insiders:
            conn.execute("INSERT INTO case_wall_insiders(case_id, principal_name, added_by, added_at) VALUES (?, ?, ?, ?)",
                         (case_id, name, principal.name, stamp))
        _audit(conn, case_id, "调整隔离墙" if existed else "设立隔离墙",
               f"{principal.name}：墙内 {len(insiders)} 人；理由：{body.reason.strip()}")
        wall = _wall(conn, case_id)
    record_security_event("case_wall", "updated" if existed else "created", actor=principal.name, case_id=case_id,
                          detail=f"insiders:{len(insiders)}", request_path=f"/api/case-walls/{case_id}")
    return wall or {}


@router.delete("/api/case-walls/{case_id}")
def remove_wall(case_id: int, reason: str = Query(min_length=4, max_length=500)) -> dict[str, Any]:
    principal = _require_admin()
    with system_access(), transaction() as conn:
        if conn.execute("DELETE FROM case_walls WHERE case_id = ?", (case_id,)).rowcount == 0:
            raise HTTPException(404, "该案件没有隔离墙")
        _audit(conn, case_id, "撤除隔离墙", f"{principal.name}：理由：{reason.strip()}")
    record_security_event("case_wall", "removed", actor=principal.name, case_id=case_id,
                          request_path=f"/api/case-walls/{case_id}")
    return {"case_id": case_id, "removed": True}


@router.post("/api/case-walls/{case_id}/break-glass", status_code=201)
def break_glass(case_id: int, body: BreakGlassIn) -> dict[str, Any]:
    """Time-limited access for an administrator outside the wall, always audited."""
    principal = _require_admin()
    expires_at = int(time.time()) + BREAK_GLASS_SECONDS
    with system_access(), transaction() as conn:
        wall = _wall(conn, case_id)
        if wall is None:
            raise HTTPException(404, "该案件没有隔离墙")
        if principal.name in wall["insiders"]:
            raise HTTPException(409, "已在墙内，不需要破窗")
        conn.execute(
            "INSERT INTO case_wall_grants(case_id, principal_name, reason, granted_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (case_id, principal.name, body.reason.strip(), now(), expires_at),
        )
        _audit(conn, case_id, "隔离墙破窗访问",
               f"{principal.name}：{BREAK_GLASS_SECONDS // 60} 分钟；理由：{body.reason.strip()}")
    record_security_event("wall_break_glass", "granted", actor=principal.name, case_id=case_id,
                          detail=f"expires_at:{expires_at}", request_path=f"/api/case-walls/{case_id}/break-glass")
    return {"case_id": case_id, "principal": principal.name, "expires_at": expires_at}
