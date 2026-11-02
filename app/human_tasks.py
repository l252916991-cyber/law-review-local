"""Human review tasks: assignment, due dates and a closed state machine.

The AI review queue in ``review_jobs`` is deliberately separate: these tasks are
work a person owes another person. Every transition writes an event row, so a
returned task keeps the reason and the actor visible after it is reworked.

States: 待处理 → 进行中 → 待复核 → 已完成；待处理/待复核 可退回待处理；
未完结任务可取消（已取消）。
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .db import connect, now, record_security_event, transaction
from .security import (
    MANAGER_ROLES,
    REVIEW_ROLES,
    ROLE_PERMISSIONS,
    current_principal,
    has_case_role,
    require_case_access,
)

router = APIRouter(prefix="/api", tags=["tasks"])

TASK_KINDS = ("阅卷", "核证", "补证", "复核", "归档", "其他")
OPEN_STATUSES = ("待处理", "进行中", "待复核")
STATUS_LABELS = {
    "待处理": "待处理",
    "进行中": "进行中",
    "待复核": "待复核",
    "已完成": "已完成",
    "已取消": "已取消",
}


def _parse_due(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(400, "期限格式应为 YYYY-MM-DD") from exc
    return parsed.isoformat()


def _is_manager(case_id: int) -> bool:
    """Managing people, dates and cancellation needs edit rights *and* 承办."""
    return has_case_role("edit", case_id, MANAGER_ROLES)


def _is_reviewer(case_id: int) -> bool:
    """Returning or completing a task is a review action, not just a role."""
    return has_case_role("approve", case_id, REVIEW_ROLES)


def _can_work(case_id: int, task: Any) -> bool:
    """Starting or submitting needs edit rights on top of the assignment."""
    principal = current_principal()
    if not principal.has_for_case("edit", case_id):
        return False
    return _is_assignee(task) or _is_manager(case_id)


def _is_assignee(task: Any) -> bool:
    principal = current_principal()
    if principal.member_id is not None and task["assignee_member_id"] is not None:
        return principal.member_id == task["assignee_member_id"]
    return bool(task["assignee_name"]) and task["assignee_name"] == principal.name


def _can_assign(row) -> bool:
    if row["is_admin"]:
        return True
    needed = {"view", "edit"}
    return (needed <= set(json.loads(row["permissions_json"] or "[]"))
            and needed <= ROLE_PERMISSIONS.get(row["role"], frozenset()))


def _assignee_rows(conn, case_id: int):
    return conn.execute(
        "SELECT m.id, m.name, m.active, m.is_admin, m.permissions_json, cm.role "
        "FROM members m LEFT JOIN case_members cm ON cm.member_id=m.id AND cm.case_id=? "
        "ORDER BY m.name, m.id", (case_id,),
    ).fetchall()


def _member_name(conn, member_id: int | None, case_id: int) -> str:
    if member_id is None:
        return ""
    row = next((item for item in _assignee_rows(conn, case_id) if item["id"] == member_id), None)
    if not row:
        raise HTTPException(404, "指派成员不存在")
    if not row["active"]:
        raise HTTPException(409, "指派成员已停用")
    if not _can_assign(row):
        raise HTTPException(409, "任务负责人必须有本案查看和编辑权限，请先完成案件分工与授权")
    return row["name"]


def _task_payload(conn, row, *, today: str) -> dict[str, Any]:
    case_id = row["case_id"]
    events = conn.execute(
        "SELECT action, actor, note, from_status, to_status, created_at FROM review_task_events "
        "WHERE task_id = ? ORDER BY id", (row["id"],)
    ).fetchall()
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "title": row["title"],
        "kind": row["kind"],
        "detail": row["detail"],
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "assignee_member_id": row["assignee_member_id"],
        "assignee_name": row["assignee_name"],
        "due_at": row["due_at"],
        "overdue": bool(row["due_at"] and row["status"] in OPEN_STATUSES and row["due_at"] < today),
        "evidence_id": row["evidence_id"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
        "version": row["version"],
        # The interface mirrors these server-side decisions; a hidden button is
        # never the authorization, only the explanation of a refused action.
        "can_manage": _is_manager(case_id),
        "can_review": _is_reviewer(case_id),
        "can_work": _can_work(case_id, row),
        "events": [
            {"action": item["action"], "actor": item["actor"], "note": item["note"],
             "from": item["from_status"], "to": item["to_status"], "created_at": item["created_at"]}
            for item in events
        ],
    }


def _audit(conn, case_id: int, task: Any, action: str, actor: str, note: str = "") -> None:
    detail = f"任务#{task['id']}《{task['title']}》 {action} 操作人[{actor}]"
    if note:
        detail += f"；说明：{note[:200]}"
    conn.execute(
        "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '任务流转', ?, ?)",
        (case_id, detail, now()),
    )


def _load_task(conn, task_id: int):
    row = conn.execute("SELECT * FROM review_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    return row


def _transition(conn, row, *, to_status: str, action: str, actor: str, note: str = "") -> None:
    ts = now()
    changed = conn.execute(
        "UPDATE review_tasks SET status = ?, updated_at = ?, closed_at = ?, version = version + 1 "
        "WHERE id = ? AND version = ? AND status = ?",
        (to_status, ts, ts if to_status in {"已完成", "已取消"} else None, row["id"], row["version"], row["status"]),
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "任务已被其他人修改，请刷新后重试")
    conn.execute(
        "INSERT INTO review_task_events(task_id, from_status, to_status, action, actor, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (row["id"], row["status"], to_status, action, actor, note[:500], ts),
    )


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    kind: str = "阅卷"
    detail: str = Field(default="", max_length=2000)
    assignee_member_id: int | None = Field(default=None, ge=1)
    due_at: str | None = None
    evidence_id: int | None = Field(default=None, ge=1)


class TaskAction(BaseModel):
    action: Literal["start", "submit", "return", "complete", "cancel", "assign", "due"]
    note: str = Field(default="", max_length=500)
    assignee_member_id: int | None = Field(default=None, ge=1)
    due_at: str | None = None
    expected_version: int | None = Field(default=None, ge=1)


@router.get("/cases/{case_id}/task-assignees")
def task_assignees(case_id: int):
    """Task candidates must be able to read and work this case; assignment grants no access."""
    if not _is_manager(case_id):
        raise HTTPException(403, "需要编辑权限及案件负责人角色")
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone():
            raise HTTPException(404, "案件不存在")
        return {"assignees": [{"member_id": row["id"], "name": row["name"]}
                              for row in _assignee_rows(conn, case_id) if row["active"] and _can_assign(row)]}


@router.get("/cases/{case_id}/tasks")
def list_case_tasks(case_id: int, status: str | None = None, mine: bool = False):
    require_case_access(case_id)
    principal = current_principal()
    today = date.today().isoformat()
    clauses = ["case_id = ?"]
    params: list[Any] = [case_id]
    if status:
        if status not in STATUS_LABELS:
            raise HTTPException(400, "未知的任务状态")
        clauses.append("status = ?")
        params.append(status)
    if mine:
        if principal.member_id is None:
            return {"tasks": [], "counts": {key: 0 for key in STATUS_LABELS}}
        clauses.append("assignee_member_id = ?")
        params.append(principal.member_id)
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM review_tasks WHERE {' AND '.join(clauses)} ORDER BY "
            "CASE status WHEN '待处理' THEN 1 WHEN '进行中' THEN 2 WHEN '待复核' THEN 3 ELSE 4 END, "
            "due_at IS NULL, due_at, id",
            params,
        ).fetchall()
        counts = {key: 0 for key in STATUS_LABELS}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {"tasks": [_task_payload(conn, row, today=today) for row in rows], "counts": counts}


@router.post("/cases/{case_id}/tasks", status_code=201)
def create_task(case_id: int, payload: TaskCreate):
    if not _is_manager(case_id):
        raise HTTPException(403, "需要编辑权限及案件负责人角色")
    if payload.kind not in TASK_KINDS:
        raise HTTPException(400, "任务类型必须来自：" + "、".join(TASK_KINDS))
    due = _parse_due(payload.due_at)
    actor = current_principal().name
    with transaction() as conn:
        case = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "案件不存在")
        assignee = _member_name(conn, payload.assignee_member_id, case_id)
        if payload.evidence_id is not None:
            evidence = conn.execute("SELECT case_id FROM evidence WHERE id = ?", (payload.evidence_id,)).fetchone()
            if not evidence or evidence["case_id"] != case_id:
                raise HTTPException(404, "关联证据不在本案")
        ts = now()
        task_id = conn.execute(
            "INSERT INTO review_tasks(case_id, kind, title, detail, assignee_member_id, assignee_name, status, "
            "due_at, evidence_id, created_by, created_at, updated_at, version) "
            "VALUES (?,?,?,?,?,?,'待处理',?,?,?,?,?,1)",
            (case_id, payload.kind, payload.title, payload.detail, payload.assignee_member_id, assignee,
             due, payload.evidence_id, actor, ts, ts),
        ).lastrowid
        conn.execute(
            "INSERT INTO review_task_events(task_id, from_status, to_status, action, actor, note, created_at) "
            "VALUES (?, '', '待处理', 'create', ?, ?, ?)",
            (task_id, actor, "指派给 " + assignee if assignee else "待指派", ts),
        )
        row = conn.execute("SELECT * FROM review_tasks WHERE id = ?", (task_id,)).fetchone()
        body = _task_payload(conn, row, today=date.today().isoformat())
    record_security_event("task", "success", actor=actor, case_id=case_id, detail=f"创建任务#{task_id}")
    return {"task": body}


@router.get("/tasks/mine")
def my_tasks():
    principal = current_principal()
    if principal.member_id is None:
        return {"tasks": [], "counts": {key: 0 for key in STATUS_LABELS}}
    allowed = principal.case_ids if not principal.admin else None
    today = date.today().isoformat()
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM review_tasks WHERE assignee_member_id = ? AND status IN ('待处理','进行中','待复核') "
            "ORDER BY due_at IS NULL, due_at, id",
            (principal.member_id,),
        ).fetchall()
        from .case_walls import hidden_case_ids

        hidden = hidden_case_ids()
        visible = [row for row in rows if (allowed is None or row["case_id"] in allowed)
                   and row["case_id"] not in hidden]
        tasks = [_task_payload(conn, row, today=today) for row in visible]
    counts = {key: 0 for key in STATUS_LABELS}
    for task in tasks:
        counts[task["status"]] += 1
    return {"tasks": tasks, "counts": counts}


@router.patch("/tasks/{task_id}")
def act_on_task(task_id: int, payload: TaskAction):
    principal = current_principal()
    with transaction() as conn:
        row = _load_task(conn, task_id)
        case_id = row["case_id"]
        require_case_access(case_id)
        actor = principal.name
        action = payload.action

        if action in {"assign", "due", "cancel"} and not _is_manager(case_id):
            raise HTTPException(403, "需要编辑权限及案件负责人角色")
        if action in {"return", "complete"} and not _is_reviewer(case_id):
            raise HTTPException(403, "需要审批确认权限及复核律师或案件负责人角色")
        if action in {"start", "submit"} and not _can_work(case_id, row):
            raise HTTPException(403, "需要编辑权限且为任务负责人或案件负责人")

        if payload.expected_version is not None and payload.expected_version != row["version"]:
            raise HTTPException(409, "任务已被其他人修改，请刷新后重试")

        if row["status"] in {"已完成", "已取消"}:
            raise HTTPException(409, "任务已结束，不能再操作")

        if action == "start":
            if row["status"] != "待处理":
                raise HTTPException(409, "只有待处理任务可以开始")
            if not row["assignee_member_id"] and not row["assignee_name"]:
                raise HTTPException(409, "请先指派任务负责人")
            _transition(conn, row, to_status="进行中", action="start", actor=actor)
        elif action == "submit":
            if row["status"] != "进行中":
                raise HTTPException(409, "只有进行中的任务可以提交复核")
            _transition(conn, row, to_status="待复核", action="submit", actor=actor, note=payload.note)
        elif action == "return":
            note = payload.note.strip()
            if not note:
                raise HTTPException(400, "退回任务必须填写原因")
            if row["status"] not in {"待复核", "进行中"}:
                raise HTTPException(409, "只有待复核或进行中的任务可以退回")
            _transition(conn, row, to_status="待处理", action="return", actor=actor, note=note)
        elif action == "complete":
            if row["status"] != "待复核":
                raise HTTPException(409, "只有待复核的任务可以确认完成")
            _transition(conn, row, to_status="已完成", action="complete", actor=actor, note=payload.note)
        elif action == "cancel":
            _transition(conn, row, to_status="已取消", action="cancel", actor=actor, note=payload.note)
        elif action == "assign":
            if payload.assignee_member_id is None:
                raise HTTPException(400, "请选择要指派的成员")
            name = _member_name(conn, payload.assignee_member_id, case_id)
            changed = conn.execute(
                "UPDATE review_tasks SET assignee_member_id = ?, assignee_name = ?, updated_at = ?, "
                "version = version + 1 WHERE id = ? AND version = ? AND status = ?",
                (payload.assignee_member_id, name, now(), task_id, row["version"], row["status"]),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "任务已被其他人修改，请刷新后重试")
            conn.execute(
                "INSERT INTO review_task_events(task_id, from_status, to_status, action, actor, note, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (task_id, row["status"], row["status"], "assign", actor, f"指派给 {name}", now()),
            )
        else:  # due
            due = _parse_due(payload.due_at)
            changed = conn.execute(
                "UPDATE review_tasks SET due_at = ?, updated_at = ?, version = version + 1 "
                "WHERE id = ? AND version = ? AND status = ?",
                (due, now(), task_id, row["version"], row["status"]),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "任务已被其他人修改，请刷新后重试")
            conn.execute(
                "INSERT INTO review_task_events(task_id, from_status, to_status, action, actor, note, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (task_id, row["status"], row["status"], "due", actor, f"期限 {due or '清除'}", now()),
            )

        _audit(conn, case_id, row, action, actor, payload.note)
        updated = conn.execute("SELECT * FROM review_tasks WHERE id = ?", (task_id,)).fetchone()
        body = _task_payload(conn, updated, today=date.today().isoformat())
    record_security_event("task", "success", actor=principal.name, case_id=case_id,
                          detail=f"{action} 任务#{task_id}")
    return {"task": body}
