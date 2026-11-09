"""Case lifecycle, detail, audit trail and platform metrics endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.routes.common import require_case
from app.api.schemas import CaseDetail, CaseRow, CaseSummary
from app.cases.case_walls import exclusion_clause, hidden_case_ids
from app.cases.conflicts import PartyIn, replace_parties, run_check
from app.core.audit_trail import record_field_changes
from app.core.security import actor_name, allowed_case_ids, identity, require_permission
from app.review.services import rowdict
from app.storage.db import connect, now, record_security_event, transaction

router = APIRouter()


class CaseCreate(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    case_no: str = ""
    case_type: str = "刑事"
    client_name: str = ""
    description: str = ""
    # Parties given at creation are checked for conflicts of interest at once.
    parties: list[PartyIn] = Field(default_factory=list, max_length=50)


class CaseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=120)
    case_no: str | None = Field(default=None, max_length=60)
    case_type: str | None = Field(default=None, max_length=40)
    client_name: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=5000)
    expected_version: int | None = Field(default=None, ge=1)


@router.get("/api/cases", response_model=list[CaseSummary])
def list_cases():
    conn = connect()
    try:
        permitted = allowed_case_ids()
        where = ""
        parameters: tuple = ()
        if permitted is not None:
            if not permitted:
                return []
            where = " WHERE c.id IN (" + ",".join("?" for _ in permitted) + ")"
            parameters = tuple(permitted)
        walled, walled_parameters = exclusion_clause("c.id", hidden_case_ids())
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM documents d WHERE d.case_id = c.id) AS document_count,
                   (SELECT COALESCE(SUM(pages), 0) FROM documents d WHERE d.case_id = c.id) AS page_count,
                   (SELECT COUNT(*) FROM evidence e WHERE e.case_id = c.id) AS evidence_count
            FROM cases c
            """ + (" WHERE c.lifecycle_status = 'active' AND c.lifecycle_status IS NOT NULL" + (" AND " + where[7:] if where else ""))
            + walled + " ORDER BY c.updated_at DESC, c.id DESC", parameters + walled_parameters,
        ).fetchall()
        return [rowdict(x) for x in rows]
    finally:
        conn.close()


@router.post("/api/cases", status_code=201)
def create_case(payload: CaseCreate):
    ts = now()
    with transaction() as conn:
        case_id = conn.execute(
            """
            INSERT INTO cases(title, case_no, case_type, client_name, status, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, '阅卷中', ?, ?, ?)
            """,
            (payload.title, payload.case_no, payload.case_type, payload.client_name, payload.description, ts, ts),
        ).lastrowid
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '创建案件', ?, ?)",
            (case_id, payload.title, ts),
        )
        if payload.parties:
            replace_parties(conn, case_id, payload.parties, actor_name())
    case = require_case(case_id)
    if payload.parties:
        case["conflict"] = run_check(case_id, actor_name())
    return case


@router.patch("/api/cases/{case_id}")
def update_case(case_id: int, payload: CaseUpdate):
    """修改案件基本信息（带版本校验与字段级审计）。

    建案后案号、当事人、类型和说明都可能需要更正；这里保留同一案件 ID，
    不通过重建案件来"修正"信息。带上 ``expected_version`` 可避免覆盖同事
    刚保存的修改。
    """
    require_permission("edit")
    current = require_case(case_id)
    if payload.expected_version is not None and payload.expected_version != current["version"]:
        raise HTTPException(409, f"案件已被他人修改（当前版本 {current['version']}），请刷新后重试")
    values = {
        key: value
        for key, value in payload.model_dump(exclude_none=True).items()
        if key != "expected_version"
    }
    if not values:
        raise HTTPException(400, "至少需要更新一个字段")
    if "title" in values and not values["title"].strip():
        raise HTTPException(400, "案件名称不能为空")
    with transaction() as conn:
        assignments = ", ".join(f"{key} = ?" for key in values)
        cursor = conn.execute(
            f"UPDATE cases SET {assignments}, updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
            (*values.values(), now(), case_id, current["version"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(409, "案件已被他人修改，请刷新后重试")
        record_field_changes(
            conn, case_id=case_id, action="修改案件信息", target=f"案件#{case_id}《{current['title']}》",
            actor=actor_name(), before={key: current[key] for key in values}, after=values,
            versions=(current["version"], current["version"] + 1),
        )
    return require_case(case_id)


@router.post("/api/cases/{case_id}/archive")
def archive_case(case_id: int):
    require_permission("edit")
    case = require_case(case_id)
    if case.get("lifecycle_status") != "active":
        raise HTTPException(409, "案件当前不在正常工作区")
    ts = now()
    actor = identity().get("name")
    with transaction() as conn:
        conn.execute("UPDATE cases SET lifecycle_status='archived', archived_at=?, archived_by=?, updated_at=? WHERE id=?", (ts, actor, ts, case_id))
        conn.execute("INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,?,?,?)", (case_id, "归档案卷", "长期保留", ts))
    return require_case(case_id)


@router.post("/api/cases/{case_id}/restore")
def restore_case(case_id: int):
    require_permission("edit")
    case = require_case(case_id)
    if case.get("lifecycle_status") not in {"archived", "trash"}:
        raise HTTPException(409, "案件当前不可恢复")
    ts = now()
    with transaction() as conn:
        conn.execute("UPDATE cases SET lifecycle_status='active', archived_at=NULL, archived_by=NULL, trashed_at=NULL, purge_after=NULL, updated_at=? WHERE id=?", (ts, case_id))
        conn.execute("INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,?,?,?)", (case_id, "恢复案卷", "恢复到正常工作区", ts))
    return require_case(case_id)


@router.post("/api/cases/{case_id}/trash")
def trash_case(case_id: int):
    require_permission("edit")
    case = require_case(case_id)
    if case.get("lifecycle_status") not in {"active", "archived"}:
        raise HTTPException(409, "案件已在回收站")
    ts = datetime.now().astimezone(); purge = (ts + timedelta(days=30)).isoformat(timespec="seconds")
    with transaction() as conn:
        conn.execute("UPDATE cases SET lifecycle_status='trash', trashed_at=?, purge_after=?, updated_at=? WHERE id=?", (ts.isoformat(timespec="seconds"), purge, ts.isoformat(timespec="seconds"), case_id))
        conn.execute("INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,?,?,?)", (case_id, "移入回收站", "30天后可永久清理", ts.isoformat(timespec="seconds")))
    record_security_event("case_trash", "success", actor=identity().get("name"), case_id=case_id, detail="retention_days=30", request_path=f"/api/cases/{case_id}/trash")
    return require_case(case_id)


@router.get("/api/cases/lifecycle/{lifecycle_status}", response_model=list[CaseRow])
def lifecycle_cases(lifecycle_status: Literal["archived", "trash"]):
    require_permission("view")
    conn = connect()
    try:
        permitted = allowed_case_ids()
        if permitted == ():
            return []
        clause = ""
        parameters: tuple = (lifecycle_status,)
        if permitted is not None:
            clause = " AND id IN (" + ",".join("?" for _ in permitted) + ")"
            parameters += tuple(permitted)
        walled, walled_parameters = exclusion_clause("id", hidden_case_ids())
        clause += walled
        parameters += walled_parameters
        return [rowdict(r) for r in conn.execute(
            "SELECT * FROM cases WHERE lifecycle_status=?" + clause + " ORDER BY updated_at DESC", parameters
        ).fetchall()]
    finally: conn.close()


@router.get("/api/cases/{case_id}", response_model=CaseDetail)
def case_detail(case_id: int):
    case = require_case(case_id)
    conn = connect()
    try:
        case["metrics"] = {
            "documents": conn.execute("SELECT COUNT(*) FROM documents WHERE case_id = ?", (case_id,)).fetchone()[0],
            "pages": conn.execute("SELECT COALESCE(SUM(pages), 0) FROM documents WHERE case_id = ?", (case_id,)).fetchone()[0],
            "evidence": conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id = ?", (case_id,)).fetchone()[0],
            "confirmed": conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id = ? AND status = '已确认'", (case_id,)).fetchone()[0],
        }
        return case
    finally:
        conn.close()


@router.get("/api/cases/{case_id}/platform-metrics")
def case_platform_metrics(case_id: int):
    require_case(case_id)
    from app.evaluation.rag_eval import platform_metrics

    return platform_metrics(case_id)


@router.get("/api/cases/{case_id}/audit")
def audit(case_id: int):
    require_case(case_id)
    conn = connect()
    try:
        return [
            rowdict(x)
            for x in conn.execute("SELECT * FROM audit_log WHERE case_id = ? ORDER BY id DESC LIMIT 100", (case_id,))
        ]
    finally:
        conn.close()

