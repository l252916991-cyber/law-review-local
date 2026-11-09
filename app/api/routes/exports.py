"""Case export package and export template endpoints."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.routes.common import require_case
from app.cases.exports import build_export_package, get_export_template
from app.core.security import actor_name, require_permission
from app.storage.db import connect, now, record_security_event, transaction

router = APIRouter()


@router.get("/api/cases/{case_id}/export")
def export_case(case_id: int, request: Request, template_id: int | None = None, final: bool = False,
                report_id: int | None = None):
    require_case(case_id)
    try:
        path, package_sha = build_export_package(
            case_id, template_id=template_id, final=final, actor=actor_name(), report_id=report_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    record_security_event("case_export", "success", actor=actor_name(), case_id=case_id,
                          detail=path.name, request_path=request.url.path)
    return FileResponse(path, media_type="application/zip", filename=path.name,
                        headers={"X-Package-Sha256": package_sha})


class ExportTemplateBlockIn(BaseModel):
    type: Literal["case_summary", "catalog_csv", "evidence_table", "qa_log", "static_markdown", "attachments"]
    title: str = Field(min_length=1, max_length=60)
    filename: str | None = Field(default=None, max_length=120)
    content: str = Field(default="", max_length=20000)
    evidence_status: Literal["待复核", "已确认", "待质证", "待补证"] | None = None
    required: bool = False


class ExportTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    blocks: list[ExportTemplateBlockIn] = Field(min_length=1, max_length=12)


def _reject_immutable_builtin(template: dict[str, Any] | None) -> dict[str, Any]:
    if template is None:
        raise HTTPException(404, "结案模板不存在")
    if template["builtin"]:
        raise HTTPException(409, "内置模板不可修改或删除")
    return template


@router.get("/api/export-templates")
def list_export_templates():
    conn = connect()
    try:
        return [
            {"id": row["id"], "name": row["name"], "description": row["description"],
             "builtin": bool(row["builtin"]), "updated_at": row["updated_at"]}
            for row in conn.execute("SELECT * FROM export_templates ORDER BY builtin DESC, id").fetchall()
        ]
    finally:
        conn.close()


@router.get("/api/export-templates/{template_id}")
def get_export_template_detail(template_id: int):
    require_permission("manage")
    template = get_export_template(template_id)
    if template is None:
        raise HTTPException(404, "结案模板不存在")
    return template


@router.post("/api/export-templates", status_code=201)
def create_export_template(body: ExportTemplateIn, request: Request):
    require_permission("manage")
    for block in body.blocks:
        if block.type == "static_markdown" and not block.content.strip():
            raise HTTPException(400, "静态说明区块必须填写内容")
        if block.type != "static_markdown" and block.type != "evidence_table" and block.evidence_status is not None:
            raise HTTPException(400, "仅证据表区块支持状态过滤")
    ts = now()
    with transaction() as conn:
        try:
            template_id = conn.execute(
                """INSERT INTO export_templates(name, description, blocks_json, builtin, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (body.name.strip(), body.description, json.dumps([b.model_dump() for b in body.blocks], ensure_ascii=False),
                 actor_name(), ts, ts),
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "同名结案模板已存在") from exc
    record_security_event("config", "export_template_created", actor=actor_name(), detail=body.name.strip(),
                          request_path=request.url.path)
    return {"id": template_id, "name": body.name.strip()}


@router.put("/api/export-templates/{template_id}")
def update_export_template(template_id: int, body: ExportTemplateIn, request: Request):
    require_permission("manage")
    _reject_immutable_builtin(get_export_template(template_id))
    for block in body.blocks:
        if block.type == "static_markdown" and not block.content.strip():
            raise HTTPException(400, "静态说明区块必须填写内容")
    with transaction() as conn:
        updated = conn.execute(
            "UPDATE export_templates SET name=?, description=?, blocks_json=?, updated_at=? WHERE id=? AND builtin=0",
            (body.name.strip(), body.description, json.dumps([b.model_dump() for b in body.blocks], ensure_ascii=False), now(), template_id),
        ).rowcount
    if not updated:
        raise HTTPException(404, "结案模板不存在")
    record_security_event("config", "export_template_updated", actor=actor_name(), detail=body.name.strip(),
                          request_path=request.url.path)
    return {"id": template_id, "name": body.name.strip()}


@router.delete("/api/export-templates/{template_id}")
def delete_export_template(template_id: int, request: Request):
    require_permission("manage")
    template = _reject_immutable_builtin(get_export_template(template_id))
    with transaction() as conn:
        conn.execute("DELETE FROM export_templates WHERE id=? AND builtin=0", (template_id,))
    record_security_event("config", "export_template_deleted", actor=actor_name(), detail=template["name"],
                          request_path=request.url.path)
    return {"deleted": True}

