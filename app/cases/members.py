"""Local member directory, hashed access tokens and per-case roles.

Environment tokens (``LAW_REVIEW_API_TOKENS_JSON``) remain the deployment
bootstrap. This module adds day-to-day administration: an administrator can add
a colleague, hand out or rotate a token, assign cases, and revoke access without
editing environment files or restarting the process.

Security properties kept on purpose:

- Only SHA-256 digests of tokens are stored; the plaintext is returned once at
  creation/rotation and never written to the database, logs or audit trail.
- A member is deactivated, never deleted, so case assignments, audit rows and
  task history keep one stable identity.
- Every request resolves the member from the database, so deactivation or a
  permission change takes effect on the next request, including for sessions
  that were already issued.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.audit_trail import record_field_changes
from app.core.security import (
    MEMBER_PERMISSIONS,
    PERMISSION_LABELS,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    AccessConfigurationError,
    Principal,
    actor_name,
    auth_mode,
    configured_tokens,
    current_principal,
    require_permission,
)
from app.storage.db import connect, now, record_security_event, transaction

router = APIRouter(prefix="/api", tags=["members"])

TOKEN_BYTES = 32
CASE_ROLES = tuple(ROLE_PERMISSIONS)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_principal(conn, row) -> Principal:
    assignments = conn.execute(
        "SELECT case_id, role FROM case_members WHERE member_id = ? ORDER BY case_id", (row["id"],)
    ).fetchall()
    permissions = frozenset(json.loads(row["permissions_json"] or "[]"))
    return Principal(
        name=row["name"],
        admin=bool(row["is_admin"]),
        case_ids=tuple(item["case_id"] for item in assignments),
        permissions=permissions,
        member_id=row["id"],
        roles=tuple((item["case_id"], item["role"]) for item in assignments),
    )


def principal_for_fingerprint(fingerprint: str) -> Principal | None:
    """Resolve an active member from a token digest (session or bearer token)."""
    if not fingerprint:
        return None
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT m.* FROM member_tokens t JOIN members m ON m.id = t.member_id
               WHERE t.token_hash = ? AND t.revoked_at IS NULL AND m.active = 1""",
            (fingerprint,),
        ).fetchone()
        return _load_principal(conn, row) if row else None


def principal_for_token(token: str) -> Principal | None:
    return principal_for_fingerprint(_token_hash(token))


def active_member_count() -> int:
    with closing(connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM members WHERE active = 1").fetchone()[0]


def require_case_manager(case_id: int) -> None:
    """Only an administrator or the case's 承办律师 may change its people."""
    principal = current_principal()
    if principal.admin or principal.role_for(case_id) == "owner":
        return
    raise HTTPException(403, "需要案件负责人或管理员权限")


def _has_environment_admin() -> bool:
    try:
        return any(principal.admin for principal in configured_tokens().values())
    except AccessConfigurationError:
        return False


def _require_token_mode() -> None:
    if auth_mode() != "token":
        raise HTTPException(409, "本机模式为单用户，成员管理仅在 token 身份模式启用")


def _configured_members() -> list[dict[str, Any]]:
    """Environment principals are shown read-only next to directory members."""
    try:
        tokens = configured_tokens()
    except AccessConfigurationError:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for principal in tokens.values():
        seen.setdefault(principal.name, {
            "name": principal.name,
            "admin": principal.admin,
            "permissions": sorted(principal.permissions),
            "case_ids": list(principal.case_ids),
            "source": "config",
        })
    return [seen[name] for name in sorted(seen)]


def _member_payload(conn, row) -> dict[str, Any]:
    from app.cases.case_walls import exclusion_clause, hidden_case_ids

    clause, parameters = exclusion_clause("cm.case_id", hidden_case_ids())
    assignments = conn.execute(
        """SELECT cm.case_id, cm.role, c.title FROM case_members cm
           JOIN cases c ON c.id = cm.case_id WHERE cm.member_id = ?""" + clause + " ORDER BY cm.case_id",
        (row["id"], *parameters),
    ).fetchall()
    active_tokens = conn.execute(
        "SELECT COUNT(*) FROM member_tokens WHERE member_id = ? AND revoked_at IS NULL", (row["id"],)
    ).fetchone()[0]
    return {
        "id": row["id"],
        "name": row["name"],
        "admin": bool(row["is_admin"]),
        "permissions": sorted(json.loads(row["permissions_json"] or "[]")),
        "active": bool(row["active"]),
        "case_ids": [item["case_id"] for item in assignments],
        "cases": [{"id": item["case_id"], "title": item["title"], "role": item["role"]} for item in assignments],
        "active_tokens": active_tokens,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _role_choices() -> list[dict[str, str]]:
    return [{"key": key, "label": ROLE_LABELS[key]} for key in CASE_ROLES]


def _permission_choices() -> list[dict[str, str]]:
    return [{"key": key, "label": PERMISSION_LABELS[key]} for key in sorted(MEMBER_PERMISSIONS)]


def _validate_permissions(permissions: list[str]) -> frozenset[str]:
    values = frozenset(permissions)
    if not values or not values <= MEMBER_PERMISSIONS:
        raise HTTPException(400, "成员权限必须来自：" + "、".join(sorted(MEMBER_PERMISSIONS)))
    return values


def _case_exists(conn, case_id: int) -> bool:
    return conn.execute("SELECT 1 FROM cases WHERE id = ?", (case_id,)).fetchone() is not None


def _issue_token(conn, member_id: int, label: str, actor: str) -> str:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    conn.execute(
        "INSERT INTO member_tokens(member_id, token_hash, label, created_by, created_at) VALUES (?,?,?,?,?)",
        (member_id, _token_hash(token), label[:60], actor, now()),
    )
    return token


def _revoke_sessions(conn, member_id: int) -> int:
    hashes = [row[0] for row in conn.execute(
        "SELECT token_hash FROM member_tokens WHERE member_id = ?", (member_id,)
    ).fetchall()]
    removed = 0
    for token_hash in hashes:
        removed += conn.execute(
            "DELETE FROM auth_sessions WHERE credential_fingerprint = ?", (token_hash,)
        ).rowcount
    return removed


def _assignments(conn, member_id: int, assignments: list[MemberAssignmentIn], actor: str) -> None:
    for item in assignments:
        if not _case_exists(conn, item.case_id):
            raise HTTPException(404, f"案件 {item.case_id} 不存在")
        conn.execute(
            "INSERT INTO case_members(case_id, member_id, role, created_by, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(case_id, member_id) DO UPDATE SET role = excluded.role",
            (item.case_id, member_id, item.role, actor, now()),
        )


class MemberAssignmentIn(BaseModel):
    case_id: int = Field(ge=1)
    role: Literal["owner", "reviewer", "assistant", "viewer"] = "assistant"


class MemberCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    admin: bool = False
    permissions: list[str] = Field(default_factory=lambda: ["view", "edit", "approve", "export"])
    assignments: list[MemberAssignmentIn] = Field(default_factory=list, max_length=200)


class MemberUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    admin: bool | None = None
    permissions: list[str] | None = None
    active: bool | None = None


class CaseRoleIn(BaseModel):
    role: Literal["owner", "reviewer", "assistant", "viewer"]


@router.get("/members")
def list_members():
    require_permission("manage")
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM members ORDER BY id").fetchall()
        members = [_member_payload(conn, row) for row in rows]
    return {
        "mode": auth_mode(),
        "members": members,
        "configured": _configured_members(),
        "roles": _role_choices(),
        "permissions": _permission_choices(),
    }


@router.post("/members", status_code=201)
def create_member(payload: MemberCreate):
    require_permission("manage")
    _require_token_mode()
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "成员名称不能为空")
    if name in {item["name"] for item in _configured_members()}:
        raise HTTPException(409, "该名称已由部署环境中的令牌占用，请换一个名称")
    permissions = MEMBER_PERMISSIONS if payload.admin else _validate_permissions(payload.permissions)
    ts = now()
    with transaction() as conn:
        exists = conn.execute("SELECT 1 FROM members WHERE name = ?", (name,)).fetchone()
        if exists:
            raise HTTPException(409, "成员名称已存在")
        member_id = conn.execute(
            "INSERT INTO members(name, is_admin, permissions_json, active, created_by, created_at, updated_at) "
            "VALUES (?,?,?,1,?,?,?)",
            (name, int(payload.admin), json.dumps(sorted(permissions)), actor_name(), ts, ts),
        ).lastrowid
        token = _issue_token(conn, member_id, "初始令牌", actor_name())
        _assignments(conn, member_id, payload.assignments, actor_name())
        row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        body = _member_payload(conn, row)
    record_security_event("member", "success", actor=actor_name(), detail=f"创建成员 {name}")
    return {"member": body, "token": token, "notice": "访问令牌只显示这一次，请立即转交本人。"}


@router.patch("/members/{member_id}")
def update_member(member_id: int, payload: MemberUpdate):
    require_permission("manage")
    _require_token_mode()
    actor = current_principal()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        if not row:
            raise HTTPException(404, "成员不存在")
        values: dict[str, Any] = {}
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise HTTPException(400, "成员名称不能为空")
            duplicate = conn.execute("SELECT 1 FROM members WHERE name = ? AND id <> ?", (name, member_id)).fetchone()
            if duplicate:
                raise HTTPException(409, "成员名称已存在")
            values["name"] = name
        if payload.admin is not None:
            values["is_admin"] = int(payload.admin)
        if payload.permissions is not None:
            values["permissions_json"] = json.dumps(sorted(_validate_permissions(payload.permissions)))
        if payload.active is not None:
            values["active"] = int(payload.active)
        if not values:
            raise HTTPException(400, "至少需要更新一个字段")

        self_target = actor.member_id == member_id
        if self_target and (values.get("active") == 0 or values.get("is_admin") == 0):
            raise HTTPException(409, "不能停用或降级当前登录的管理员")
        losing_admin = (values.get("active") == 0 or values.get("is_admin") == 0) and bool(row["is_admin"])
        if losing_admin:
            others = conn.execute(
                "SELECT COUNT(*) FROM members WHERE active = 1 AND is_admin = 1 AND id <> ?", (member_id,)
            ).fetchone()[0]
            if others == 0 and not _has_environment_admin():
                raise HTTPException(409, "至少保留一名启用状态的管理员")

        assignments = ", ".join(f"{key} = ?" for key in values)
        conn.execute(f"UPDATE members SET {assignments}, updated_at = ? WHERE id = ?",
                     (*values.values(), now(), member_id))
        before = {
            "name": row["name"],
            "admin": bool(row["is_admin"]),
            "permissions": ",".join(sorted(json.loads(row["permissions_json"] or "[]"))),
            "active": bool(row["active"]),
        }
        after = {
            "name": values.get("name", before["name"]),
            "admin": bool(values["is_admin"]) if "is_admin" in values else before["admin"],
            "permissions": (
                ",".join(sorted(json.loads(values["permissions_json"])))
                if "permissions_json" in values else before["permissions"]
            ),
            "active": bool(values["active"]) if "active" in values else before["active"],
        }
        record_field_changes(conn, case_id=None, action="修改成员", target=f"成员#{member_id}《{before['name']}》",
                             actor=actor_name(), before=before, after=after)
        revoked = _revoke_sessions(conn, member_id) if values.get("active") == 0 else 0
        updated = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        body = _member_payload(conn, updated)
    record_security_event(
        "member", "success", actor=actor_name(), case_id=None,
        detail=f"更新成员 {body['name']}（{'停用' if body['active'] is False else '启用'}，注销会话 {revoked}）",
    )
    return {"member": body, "revoked_sessions": revoked}


@router.post("/members/{member_id}/tokens", status_code=201)
def rotate_member_token(member_id: int, label: str = ""):
    require_permission("manage")
    _require_token_mode()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        if not row:
            raise HTTPException(404, "成员不存在")
        if not row["active"]:
            raise HTTPException(409, "成员已停用，请先启用再签发令牌")
        conn.execute("UPDATE member_tokens SET revoked_at = ? WHERE member_id = ? AND revoked_at IS NULL",
                     (now(), member_id))
        removed = _revoke_sessions(conn, member_id)
        token = _issue_token(conn, member_id, label or "轮换令牌", actor_name())
        body = _member_payload(conn, row)
    record_security_event("member", "success", actor=actor_name(), detail=f"轮换令牌 {body['name']}")
    return {"member": body, "token": token, "revoked_sessions": removed,
            "notice": "旧令牌已失效，新令牌只显示这一次。"}


@router.delete("/members/{member_id}")
def deactivate_member(member_id: int):
    return update_member(member_id, MemberUpdate(active=False))


@router.get("/cases/{case_id}/members")
def case_members(case_id: int):
    require_case_manager(case_id)
    with closing(connect()) as conn:
        if not _case_exists(conn, case_id):
            raise HTTPException(404, "案件不存在")
        rows = conn.execute(
            """SELECT cm.role, cm.created_at, m.* FROM case_members cm
               JOIN members m ON m.id = cm.member_id WHERE cm.case_id = ? ORDER BY cm.role, m.name""",
            (case_id,),
        ).fetchall()
        assigned = [
            {"member_id": row["id"], "name": row["name"], "role": row["role"],
             "role_label": ROLE_LABELS.get(row["role"], row["role"]), "admin": bool(row["is_admin"]),
             "assigned_at": row["created_at"]}
            for row in rows
        ]
        candidates = [
            {"member_id": row["id"], "name": row["name"]}
            for row in conn.execute(
                """SELECT id, name FROM members WHERE active = 1 AND id NOT IN
                   (SELECT member_id FROM case_members WHERE case_id = ?) ORDER BY name""",
                (case_id,),
            ).fetchall()
        ]
    return {"assignments": assigned, "candidates": candidates, "roles": _role_choices()}


@router.put("/cases/{case_id}/members/{member_id}")
def assign_case_member(case_id: int, member_id: int, payload: CaseRoleIn):
    require_case_manager(case_id)
    _require_token_mode()
    with transaction() as conn:
        if not _case_exists(conn, case_id):
            raise HTTPException(404, "案件不存在")
        member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        if not member:
            raise HTTPException(404, "成员不存在")
        if not member["active"]:
            raise HTTPException(409, "成员已停用，不能分配案件")
        existing = conn.execute(
            "SELECT role FROM case_members WHERE case_id = ? AND member_id = ?", (case_id, member_id)
        ).fetchone()
        conn.execute(
            "INSERT INTO case_members(case_id, member_id, role, created_by, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(case_id, member_id) DO UPDATE SET role = excluded.role",
            (case_id, member_id, payload.role, actor_name(), now()),
        )
        record_field_changes(
            conn, case_id=case_id, action="调整案件分工",
            target=f"案件#{case_id} 成员《{member['name']}》", actor=actor_name(),
            before={"role": existing["role"] if existing else ""}, after={"role": payload.role},
        )
    record_security_event("member", "success", actor=actor_name(), case_id=case_id,
                          detail=f"分配 {member['name']} 为 {ROLE_LABELS[payload.role]}")
    return {"case_id": case_id, "member_id": member_id, "role": payload.role,
            "role_label": ROLE_LABELS[payload.role], "previous_role": existing["role"] if existing else None}


@router.delete("/cases/{case_id}/members/{member_id}")
def remove_case_member(case_id: int, member_id: int):
    require_case_manager(case_id)
    _require_token_mode()
    with transaction() as conn:
        if not _case_exists(conn, case_id):
            raise HTTPException(404, "案件不存在")
        existing = conn.execute(
            "SELECT role FROM case_members WHERE case_id = ? AND member_id = ?", (case_id, member_id)
        ).fetchone()
        if not existing:
            raise HTTPException(404, "该成员未分配到本案")
        conn.execute("DELETE FROM case_members WHERE case_id = ? AND member_id = ?", (case_id, member_id))
        record_field_changes(
            conn, case_id=case_id, action="调整案件分工",
            target=f"案件#{case_id} 成员#{member_id}", actor=actor_name(),
            before={"role": existing["role"]}, after={"role": ""},
        )
    record_security_event("member", "success", actor=actor_name(), case_id=case_id,
                          detail=f"移除成员 {member_id}")
    return {"case_id": case_id, "member_id": member_id, "removed": True}
