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

from app.cases.report_basis import basis_hash, basis_snapshot
from app.cases.report_delivery import (
    current_source,
    freeze_for_submission,
    load_body,
    missing_required_blocks,
    resolve_artifact_dir,
    sheet_stale,
    source_digest,
    transaction_sheet,
    verify_body,
)
from app.core.security import (
    REVIEW_ROLES,
    current_principal,
    has_case_role,
    require_case_access,
    require_case_role,
    require_permission,
)
from app.storage.db import connect, now, record_security_event, transaction

router = APIRouter(prefix="/api", tags=["reports"])

REPORT_STATUSES = ("草稿", "待复核", "已退回", "已批准", "已作废")
OPEN_STATUSES = ("草稿", "待复核", "已退回")
REVIEW_ACTIONS = {"return", "approve", "void"}


def _is_reviewer(case_id: int) -> bool:
    """A review action needs the approve permission *and* a review case role."""
    return has_case_role("approve", case_id, REVIEW_ROLES)


def _load(conn, report_id: int):
    row = conn.execute("SELECT * FROM case_reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        raise HTTPException(404, "报告不存在")
    return row


def _inspect(conn, row, current_hash: str, sheet_sha: str | None = None) -> dict[str, Any]:
    """Staleness and template completeness for one report row.

    ``stale`` means the frozen source no longer matches the live inputs;
    ``missing_blocks`` lists required template blocks that have no content, so
    the page can warn before a formal export is refused.
    """
    result: dict[str, Any] = {"stale": False, "missing_blocks": []}
    try:
        source = current_source(conn, row["case_id"], row["template_id"])
    except ValueError:
        result["stale"] = row["status"] in {"待复核", "已批准"}
        return result
    result["missing_blocks"] = missing_required_blocks(source)
    if row["status"] not in {"待复核", "已批准"}:
        return result
    if row["basis_hash"] and row["basis_hash"] != current_hash:
        result["stale"] = True
    if not result["stale"] and row["source_hash"] and source_digest(source) != row["source_hash"]:
        result["stale"] = True
    if not result["stale"] and sheet_sha is not None and row["frozen_version_id"]:
        frozen = conn.execute(
            "SELECT content_json FROM case_report_versions WHERE id = ?", (row["frozen_version_id"],)
        ).fetchone()
        if frozen is None:
            result["stale"] = True
        else:
            try:
                body = json.loads(frozen["content_json"] or "{}")
            except ValueError:
                result["stale"] = True
            else:
                result["stale"] = (body.get("transactions") or {}).get("sha256") != sheet_sha
    return result


def _payload(conn, row, current_hash: str, *, sheet_sha: str | None = None) -> dict[str, Any]:
    inspected = _inspect(conn, row, current_hash, sheet_sha)
    try:
        basis = json.loads(row["basis_json"] or "{}")
    except ValueError:
        basis = {}
    events = conn.execute(
        "SELECT action, actor, note, from_status, to_status, created_at FROM case_report_events "
        "WHERE report_id = ? ORDER BY id", (row["id"],)
    ).fetchall()
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "version": row["version"],
        "title": row["title"],
        "template_id": row["template_id"],
        "status": row["status"],
        "stale": inspected["stale"],
        "missing_blocks": inspected["missing_blocks"],
        "frozen_version_id": row["frozen_version_id"],
        "approved_version_id": row["approved_version_id"],
        "content_hash": row["content_hash"],
        "source_hash": row["source_hash"],
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
        "can_edit": current_principal().has_for_case("edit", row["case_id"]),
        "can_review": _is_reviewer(row["case_id"]),
        "can_export": current_principal().has_for_case("export", row["case_id"]),
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
        sheet_sha = transaction_sheet(case_id)["sha256"]
        reports = [_payload(conn, row, current, sheet_sha=sheet_sha) for row in rows]
    approved = next((item for item in reports if item["status"] == "已批准" and not item["stale"]), None)
    return {
        "reports": reports,
        "current_basis": {"hash": current, "counts": snapshot["counts"]},
        "deliverable": approved is not None,
        "deliverable_report_id": approved["id"] if approved else None,
    }


PREVIEW_CHARS = 200_000


@router.get("/reports/{report_id}/frozen")
def preview_frozen(report_id: int):
    """The exact body the reviewer is asked to approve.

    Read-only: it never renders from live tables, so a reviewer can compare the
    preview with what a formal export will contain. Only block text is exposed;
    artifact paths stay server-side.
    """
    with closing(connect()) as conn:
        row = _load(conn, report_id)
        require_case_access(row["case_id"])
        version = conn.execute(
            "SELECT * FROM case_report_versions WHERE id = ?", (row["frozen_version_id"],)
        ).fetchone() if row["frozen_version_id"] else None
        if version is None:
            raise HTTPException(404, "该报告还没有冻结的交付正文，请先提交复核")
        try:
            body = load_body(version)
            directory = resolve_artifact_dir(version["artifact_dir"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        blocks = []
        for item in body["blocks"]:
            path = directory / item["file"]
            if not path.is_file():
                raise HTTPException(409, f"冻结正文文件缺失：{item['archive_name']}")
            text = path.read_text(encoding="utf-8", errors="replace")
            blocks.append({
                "type": item["type"], "title": item["title"], "archive_name": item["archive_name"],
                "sha256": item["sha256"], "size": item["size"],
                "text": text[:PREVIEW_CHARS], "truncated": len(text) > PREVIEW_CHARS,
            })
        return {
            "report": {
                "id": row["id"], "version": row["version"], "title": row["title"],
                "status": row["status"], "template_id": row["template_id"],
                "frozen_version_id": version["id"], "frozen_seq": version["seq"],
                "content_hash": version["content_hash"], "source_hash": version["source_hash"],
                "frozen_at": version["frozen_at"], "frozen_by": version["frozen_by"],
            },
            "blocks": blocks,
            "transactions": {
                "archive_name": body["transactions"]["archive_name"],
                "count": body["transactions"]["count"],
                "currencies": body["transactions"]["currencies"],
                "sha256": body["transactions"]["sha256"],
            },
            "attachments": [
                {"archive_name": item["archive_name"], "size": item["size"], "sha256": item["sha256"]}
                for item in body["attachments"]
            ],
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
        # Serialize writers so two concurrent approve/return attempts resolve to
        # one success and one conflict, never a half-applied transition.
        conn.execute("BEGIN IMMEDIATE")
        row = _load(conn, report_id)
        case_id = row["case_id"]
        require_case_access(case_id)
        action = payload.action

        # Return/approve/void are review actions: the approve permission alone is
        # not enough, and a retained owner/reviewer role never restores it.
        if action in REVIEW_ACTIONS:
            require_case_role("approve", case_id, REVIEW_ROLES, "需要审批确认权限及复核律师或案件负责人角色")

        if action == "submit":
            require_permission("edit", case_id)
            if row["status"] not in {"草稿", "已退回"}:
                raise HTTPException(409, "只有草稿或已退回的报告可以提交复核")
            # The body is frozen before the status changes: an incomplete freeze
            # leaves the report a draft instead of sending reviewable-looking
            # content that cannot be delivered.
            try:
                frozen = freeze_for_submission(conn, row, actor=actor)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            snapshot = basis_snapshot(conn, case_id)
            changed = conn.execute(
                "UPDATE case_reports SET status = '待复核', basis_json = ?, basis_hash = ?, prepared_by = ?, "
                "submitted_at = ?, updated_at = ?, return_reason = '', frozen_version_id = ?, content_hash = ?, "
                "source_hash = ?, row_version = row_version + 1 WHERE id = ? AND status = ? AND row_version = ?",
                (json.dumps(snapshot, ensure_ascii=False), basis_hash(snapshot), actor, now(), now(),
                 frozen["id"], frozen["content_hash"], frozen["source_hash"], report_id, row["status"],
                 row["row_version"]),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "报告已被其他人修改，请刷新后重试")
            _event(conn, report_id, action="submit", actor=actor, from_status=row["status"], to_status="待复核")
        elif action == "return":
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
            if row["status"] != "待复核":
                raise HTTPException(409, "只有待复核的报告可以批准")
            version = conn.execute(
                "SELECT * FROM case_report_versions WHERE id = ?", (row["frozen_version_id"],)
            ).fetchone() if row["frozen_version_id"] else None
            if version is None:
                raise HTTPException(409, "该报告没有冻结的交付正文，请重新提交复核后再批准")
            # The approval covers exactly the frozen bytes: verify them and the
            # source inputs again, so an approval cannot be granted to content
            # that moved after submission.
            try:
                verify_body(version)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            try:
                current_digest = source_digest(current_source(conn, case_id, row["template_id"]))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if current_digest != version["source_hash"]:
                raise HTTPException(409, "提交复核后材料、证据、问答或模板已变化，请退回并要求重新提交")
            if sheet_stale(case_id, load_body(version)):
                raise HTTPException(409, "提交复核后资金流水已变化，请退回并要求重新提交")
            conn.execute(
                "UPDATE case_reports SET status = '已作废', superseded_by = ?, updated_at = ?, "
                "row_version = row_version + 1 WHERE case_id = ? AND status = '已批准' AND id <> ?",
                (report_id, now(), case_id, report_id),
            )
            changed = conn.execute(
                "UPDATE case_reports SET status = '已批准', reviewed_by = ?, approved_by = ?, approved_at = ?, "
                "approved_version_id = ?, content_hash = ?, source_hash = ?, updated_at = ?, "
                "row_version = row_version + 1 WHERE id = ? AND status = '待复核' AND row_version = ?",
                (actor, actor, now(), version["id"], version["content_hash"], version["source_hash"],
                 now(), report_id, row["row_version"]),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "报告已被其他人处理，请刷新后重试")
            _event(conn, report_id, action="approve", actor=actor, from_status="待复核", to_status="已批准",
                   note=payload.note)
        else:  # void
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
