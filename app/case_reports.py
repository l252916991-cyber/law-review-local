"""Case reports: draft → review → approval, bound to a material snapshot.

The export package is the report's body, so this module governs *when* that
package may be delivered: only an approved report whose snapshot still matches
the current materials may be exported as a formal delivery. Approval cannot be
reused after an evidence or document edit, and a newer approval supersedes the
older one so a delivery always points at one version.
"""
from __future__ import annotations

import json
from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .db import connect, now, record_security_event, transaction
from .report_basis import basis_hash, basis_snapshot
from .security import current_principal, require_case_access, require_permission

router = APIRouter(prefix="/api", tags=["reports"])

REPORT_STATUSES = ("草稿", "待复核", "已退回", "已批准", "已作废")
OPEN_STATUSES = ("草稿", "待复核", "已退回")


def _is_reviewer(case_id: int) -> bool:
    principal = current_principal()
    return principal.admin or principal.role_for(case_id) in {"owner", "reviewer"}


def _load(conn, report_id: int):
    row = conn.execute("SELECT * FROM case_reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        raise HTTPException(404, "报告不存在")
    return row


def _payload(conn, row, current_hash: str) -> dict[str, Any]:
    try:
        basis = json.loads(row["basis_json"] or "{}")
    except ValueError:
        basis = {}
    events = conn.execute(
        "SELECT action, actor, note, from_status, to_status, created_at FROM case_report_events "
        "WHERE report_id = ? ORDER BY id", (row["id"],)
    ).fetchall()
    day = now()[:10]
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "version": row["version"],
        "title": row["title"],
        "template_id": row["template_id"],
        "status": row["status"],
        "stale": bool(row["status"] == "已批准" and row["basis_hash"] and row["basis_hash"] != current_hash),
        "basis": {
            "hash": row["basis_hash"],
            "counts": basis.get("counts", {}),
        },
        "prepared_by": row["prepared_by"],
        "reviewed_by": row["reviewed_by"],
        "approved_by": row["approved_by"],
        "return_reason": row["return_reason"],
        "created_at": row["created_at"],
        "submitted_at": row["submitted_at"],
        "approved_at": row["approved_at"],
        "voided_at": row["voided_at"],
        "superseded_by": row["superseded_by"],
        "events": [
            {"action": item["action"], "actor": item["actor"], "note": item["note"],
             "from": item["from_status"], "to": item["to_status"], "created_at": item["created_at"]}
            for item in events
        ],
        "can_review": _is_reviewer(row["case_id"]) and day is not None,
    }


def _event(conn, report_id: int, *, action: str, actor: str, from_status: str, to_status: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO case_report_events(report_id, from_status, to_status, action, actor, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (report_id, from_status, to_status, action, actor, note[:500], now()),
    )


def _audit(conn, case_id: int, row, action: str, actor: str, note: str = "") -> None:
    detail = f"报告#{row['id']} v{row['version']}《{row['title']}》 {action} 操作人[{actor}]"
    if note:
        detail += f"；说明：{note[:200]}"
    conn.execute(
        "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '报告流转', ?, ?)",
        (case_id, detail, now()),
    )


class ReportCreate(BaseModel):
    title: str = Field(default="", max_length=120)
    template_id: int | None = Field(default=None, ge=1)


class ReportAction(BaseModel):
    action: Literal["submit", "return", "approve", "void"]
    note: str = Field(default="", max_length=500)


@router.get("/cases/{case_id}/reports")
def list_reports(case_id: int):
    require_case_access(case_id)
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM cases WHERE id = ?", (case_id,)).fetchone():
            raise HTTPException(404, "案件不存在")
        snapshot = basis_snapshot(conn, case_id)
        current = basis_hash(snapshot)
        rows = conn.execute(
            "SELECT * FROM case_reports WHERE case_id = ? ORDER BY version DESC", (case_id,)
        ).fetchall()
        reports = [_payload(conn, row, current) for row in rows]
    approved = next((item for item in reports if item["status"] == "已批准" and not item["stale"]), None)
    return {
        "reports": reports,
        "current_basis": {"hash": current, "counts": snapshot["counts"]},
        "deliverable": approved is not None,
        "deliverable_report_id": approved["id"] if approved else None,
    }


@router.post("/cases/{case_id}/reports", status_code=201)
def create_report(case_id: int, payload: ReportCreate):
    require_permission("edit", case_id)
    actor = current_principal().name
    with transaction() as conn:
        case = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise HTTPException(404, "案件不存在")
        if payload.template_id is not None:
            template = conn.execute("SELECT id FROM export_templates WHERE id = ?", (payload.template_id,)).fetchone()
            if not template:
                raise HTTPException(404, "结案模板不存在")
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM case_reports WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
        title = payload.title.strip() or f"结案报告 v{version}"
        ts = now()
        report_id = conn.execute(
            "INSERT INTO case_reports(case_id, version, title, template_id, status, basis_json, basis_hash, "
            "prepared_by, created_at, updated_at, row_version) VALUES (?,?,?,?,'草稿','{}','',?,?,?,1)",
            (case_id, version, title, payload.template_id, actor, ts, ts),
        ).lastrowid
        _event(conn, report_id, action="create", actor=actor, from_status="", to_status="草稿")
        row = conn.execute("SELECT * FROM case_reports WHERE id = ?", (report_id,)).fetchone()
        snapshot = basis_snapshot(conn, case_id)
        body = _payload(conn, row, basis_hash(snapshot))
    record_security_event("report", "success", actor=actor, case_id=case_id, detail=f"创建报告#{report_id}")
    return {"report": body}


@router.patch("/reports/{report_id}")
def act_on_report(report_id: int, payload: ReportAction):
    principal = current_principal()
    actor = principal.name
    with transaction() as conn:
        row = _load(conn, report_id)
        case_id = row["case_id"]
        require_case_access(case_id)
        action = payload.action

        if action == "submit":
            require_permission("edit", case_id)
            if row["status"] not in {"草稿", "已退回"}:
                raise HTTPException(409, "只有草稿或已退回的报告可以提交复核")
            snapshot = basis_snapshot(conn, case_id)
            conn.execute(
                "UPDATE case_reports SET status = '待复核', basis_json = ?, basis_hash = ?, prepared_by = ?, "
                "submitted_at = ?, updated_at = ?, return_reason = '', row_version = row_version + 1 WHERE id = ?",
                (json.dumps(snapshot, ensure_ascii=False), basis_hash(snapshot), actor, now(), now(), report_id),
            )
            _event(conn, report_id, action="submit", actor=actor, from_status=row["status"], to_status="待复核")
        elif action == "return":
            if not _is_reviewer(case_id):
                raise HTTPException(403, "需要复核律师或案件负责人权限")
            reason = payload.note.strip()
            if not reason:
                raise HTTPException(400, "退回报告必须填写原因")
            if row["status"] != "待复核":
                raise HTTPException(409, "只有待复核的报告可以退回")
            conn.execute(
                "UPDATE case_reports SET status = '已退回', reviewed_by = ?, return_reason = ?, "
                "updated_at = ?, row_version = row_version + 1 WHERE id = ?",
                (actor, reason, now(), report_id),
            )
            _event(conn, report_id, action="return", actor=actor, from_status="待复核", to_status="已退回", note=reason)
        elif action == "approve":
            if not _is_reviewer(case_id):
                raise HTTPException(403, "需要复核律师或案件负责人权限")
            if row["status"] != "待复核":
                raise HTTPException(409, "只有待复核的报告可以批准")
            snapshot = basis_snapshot(conn, case_id)
            if basis_hash(snapshot) != row["basis_hash"]:
                raise HTTPException(409, "提交复核后材料或证据已变化，请退回并要求重新提交")
            conn.execute(
                "UPDATE case_reports SET status = '已作废', superseded_by = ?, updated_at = ?, "
                "row_version = row_version + 1 WHERE case_id = ? AND status = '已批准' AND id <> ?",
                (report_id, now(), case_id, report_id),
            )
            conn.execute(
                "UPDATE case_reports SET status = '已批准', reviewed_by = ?, approved_by = ?, approved_at = ?, "
                "updated_at = ?, row_version = row_version + 1 WHERE id = ?",
                (actor, actor, now(), now(), report_id),
            )
            _event(conn, report_id, action="approve", actor=actor, from_status="待复核", to_status="已批准",
                   note=payload.note)
        else:  # void
            if not _is_reviewer(case_id):
                raise HTTPException(403, "需要复核律师或案件负责人权限")
            if row["status"] == "已作废":
                raise HTTPException(409, "报告已作废")
            conn.execute(
                "UPDATE case_reports SET status = '已作废', voided_at = ?, updated_at = ?, "
                "row_version = row_version + 1 WHERE id = ?",
                (now(), now(), report_id),
            )
            _event(conn, report_id, action="void", actor=actor, from_status=row["status"], to_status="已作废",
                   note=payload.note)

        _audit(conn, case_id, row, action, actor, payload.note)
        updated = conn.execute("SELECT * FROM case_reports WHERE id = ?", (report_id,)).fetchone()
        snapshot = basis_snapshot(conn, case_id)
        body = _payload(conn, updated, basis_hash(snapshot))
    record_security_event("report", "success", actor=actor, case_id=case_id, detail=f"{action} 报告#{report_id}")
    return {"report": body}
