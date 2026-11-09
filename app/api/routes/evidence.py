"""Evidence, annotation and evidence-gap analysis endpoints."""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.api.routes.common import require_case
from app.api.schemas import EvidenceList
from app.core.audit_trail import record_field_changes
from app.core.security import actor_name, require_case_access, require_permission
from app.review.services import preview_gap_analysis, rowdict, save_gap_analysis
from app.storage.db import connect, now, transaction

router = APIRouter()


class EvidenceCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200, description="证据标题")
    category: str = Field(description="证据类别")
    fact: str = Field(min_length=1, description="待证事实")
    credibility: str = Field(default="待核验", description="可信度")
    status: str = Field(default="待复核", description="审核状态")
    source_document_id: int | None = Field(default=None, description="来源文档ID")
    source_page_start: int = Field(default=1, ge=1, description="起始页码")
    source_page_end: int = Field(default=1, ge=1, description="结束页码")
    quote: str = Field(default="", description="原文引用")

    @field_validator('category')
    @classmethod
    def validate_category(cls, v):
        valid = ['书证', '物证', '言词证据', '银行流水', '审计报告',
                 '询问笔录', '电子数据', '其他材料']
        if v not in valid:
            raise ValueError(f'类别必须是: {", ".join(valid)}')
        return v

    @field_validator('credibility')
    @classmethod
    def validate_credibility(cls, v):
        valid = ['待核验', '高', '较高', '中', '低']
        if v not in valid:
            raise ValueError(f'可信度必须是: {", ".join(valid)}')
        return v

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        valid = ['待复核', '已确认', '待质证', '待补证']
        if v not in valid:
            raise ValueError(f'状态必须是: {", ".join(valid)}')
        return v


class EvidenceUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    category: str | None = None
    fact: str | None = Field(default=None, min_length=1)
    credibility: str | None = None
    status: str | None = None
    source_document_id: int | None = None
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)
    quote: str | None = None
    # Version this edit was based on. Omit for legacy clients; present it and a
    # concurrent save fails with 409 instead of overwriting someone else.
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator('category')
    @classmethod
    def validate_category(cls, v):
        if v is None:
            return v
        valid = ['书证', '物证', '言词证据', '银行流水', '审计报告',
                 '询问笔录', '电子数据', '其他材料']
        if v not in valid:
            raise ValueError(f'类别必须是: {", ".join(valid)}')
        return v

    @field_validator('credibility')
    @classmethod
    def validate_credibility(cls, v):
        if v is None:
            return v
        valid = ['待核验', '高', '较高', '中', '低']
        if v not in valid:
            raise ValueError(f'可信度必须是: {", ".join(valid)}')
        return v

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        if v is None:
            return v
        valid = ['待复核', '已确认', '待质证', '待补证']
        if v not in valid:
            raise ValueError(f'状态必须是: {", ".join(valid)}')
        return v


class AnnotationCreate(BaseModel):
    user_name: str = Field(default="本机律师", min_length=1, max_length=100)
    annotation_type: Literal["备注", "疑点", "质证意见", "补证建议", "重点", "其他"] = "备注"
    content: str = Field(min_length=1, max_length=10000)
    quote_start: int | None = Field(default=None, ge=0)
    quote_end: int | None = Field(default=None, ge=0)
    status: Literal["待处理", "已处理"] = "待处理"


class AnnotationUpdate(BaseModel):
    annotation_type: Literal["备注", "疑点", "质证意见", "补证建议", "重点", "其他"] | None = None
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    quote_start: int | None = Field(default=None, ge=0)
    quote_end: int | None = Field(default=None, ge=0)
    status: Literal["待处理", "已处理"] | None = None


@router.get("/api/cases/{case_id}/evidence", response_model=EvidenceList)
def list_evidence(case_id: int):
    require_case(case_id)
    conn = connect()
    try:
        evidence = [
            rowdict(x)
            for x in conn.execute(
                """
                SELECT e.*, d.name AS source_name FROM evidence e
                LEFT JOIN documents d ON d.id = e.source_document_id
                WHERE e.case_id = ? ORDER BY e.id
                """,
                (case_id,),
            )
        ]
        relations = [
            rowdict(x)
            for x in conn.execute(
                "SELECT * FROM evidence_relations WHERE case_id = ? ORDER BY id", (case_id,)
            )
        ]
        return {"evidence": evidence, "relations": relations}
    finally:
        conn.close()


@router.post("/api/cases/{case_id}/evidence", status_code=201)
def create_evidence(case_id: int, body: EvidenceCreate):
    """手动创建证据事项"""
    require_case(case_id)
    if body.status == "已确认":
        require_permission("approve", case_id)

    conn = connect()
    try:
        # 验证来源文档存在且属于该案件
        if body.source_document_id is not None:
            doc = conn.execute(
                "SELECT id FROM documents WHERE id = ? AND case_id = ?",
                (body.source_document_id, case_id)
            ).fetchone()
            if not doc:
                raise HTTPException(404, "来源文档不存在或不属于该案件")

        # 验证页码范围
        if body.source_page_end < body.source_page_start:
            raise HTTPException(400, "结束页码不能小于起始页码")

        cursor = conn.execute("""
            INSERT INTO evidence (
                case_id, title, category, fact, credibility, status,
                source_document_id, source_page_start, source_page_end,
                quote, approved_by, approved_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
        """, (
            case_id, body.title, body.category, body.fact, body.credibility,
            body.status, body.source_document_id, body.source_page_start,
            body.source_page_end, body.quote,
            actor_name() if body.status == "已确认" else None,
            now() if body.status == "已确认" else None
        ))

        evidence_id = cursor.lastrowid

        conn.commit()

        # 返回创建的证据
        row = conn.execute(
            """SELECT e.*, d.name AS source_name FROM evidence e
               LEFT JOIN documents d ON d.id = e.source_document_id
               WHERE e.id = ?""", (evidence_id,)
        ).fetchone()

        return {"evidence": rowdict(row), "message": "证据创建成功"}
    finally:
        conn.close()


@router.patch("/api/evidence/{evidence_id}")
def update_evidence(evidence_id: int, body: EvidenceUpdate):
    """更新证据事项

    协作约定：客户端带上读取时的 ``expected_version``；版本不一致返回 409，
    不会覆盖同事刚保存的内容。未携带时保留旧客户端行为。
    """
    conn = connect()
    try:
        # 验证证据存在
        evidence = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        if not evidence:
            raise HTTPException(404, "证据不存在")

        case_id = evidence["case_id"]
        current_version = evidence["version"]
        if body.expected_version is not None and body.expected_version != current_version:
            raise HTTPException(409, f"证据已被他人修改（当前版本 {current_version}），请刷新后重试")

        # 验证来源文档（如果要更新）
        if body.source_document_id is not None:
            doc = conn.execute(
                "SELECT id FROM documents WHERE id = ? AND case_id = ?",
                (body.source_document_id, case_id)
            ).fetchone()
            if not doc:
                raise HTTPException(404, "来源文档不存在或不属于该案件")

        values: dict[str, Any] = {}
        for field in ['title', 'category', 'fact', 'credibility', 'status',
                      'source_document_id', 'source_page_start',
                      'source_page_end', 'quote']:
            value = getattr(body, field)
            if value is not None:
                values[field] = value

        content_fields = ('title', 'category', 'fact', 'credibility',
                          'source_document_id', 'source_page_start', 'source_page_end', 'quote')
        changed_content = any(evidence[field] != values[field] for field in content_fields if field in values)
        if changed_content:
            # Approvers cannot rewrite the content they are confirming.
            require_permission("edit", case_id)

        # Approval is attributed to the authenticated principal; moving an
        # item out of 已确认 clears the attribution. Model code has no write
        # path to this endpoint, so a confirmed status always has a human name.
        approval: tuple[str | None, str | None] | None = None
        if "status" in values:
            if values["status"] == "已确认":
                require_permission("approve", case_id)
                approval = (actor_name(), now())
            else:
                approval = (None, None)
        elif evidence["status"] == "已确认" and changed_content:
            # An approval binds the exact content that was confirmed. Editing
            # the content of a confirmed item without explicitly re-confirming
            # voids the old approval and returns the item to review.
            values["status"] = "待复核"
            approval = (None, None)

        if not values:
            raise HTTPException(400, "至少需要更新一个字段")

        # 单独更新起始页或结束页时，也必须和数据库中的另一端联合校验。
        next_page_start = values.get("source_page_start", evidence["source_page_start"])
        next_page_end = values.get("source_page_end", evidence["source_page_end"])
        if next_page_end < next_page_start:
            raise HTTPException(400, "结束页码不能小于起始页码")

        updates = [f"{field} = ?" for field in values]
        params: list[Any] = list(values.values())
        if approval is not None:
            updates.extend(["approved_by = ?", "approved_at = ?"])
            params.extend(approval)
        # The conditional UPDATE is the actual guard: a read followed by another
        # writer's commit must fail here rather than silently replace it.
        params.extend([now(), evidence_id, current_version])
        cursor = conn.execute(
            f"UPDATE evidence SET {', '.join(updates)}, updated_at = ?, version = version + 1 "
            "WHERE id = ? AND version = ?",
            params,
        )
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(409, "证据已被他人修改，请刷新后重试")

        after: dict[str, Any] = dict(values)
        if approval is not None:
            after["approved_by"], after["approved_at"] = approval
        record_field_changes(
            conn, case_id=case_id, action="修改证据",
            target=f"证据#{evidence_id}《{evidence['title']}》", actor=actor_name(),
            before={field: evidence[field] for field in after}, after=after,
            versions=(current_version, current_version + 1),
        )
        conn.commit()

        # 返回更新后的证据
        row = conn.execute(
            """SELECT e.*, d.name AS source_name FROM evidence e
               LEFT JOIN documents d ON d.id = e.source_document_id
               WHERE e.id = ?""", (evidence_id,)
        ).fetchone()

        return {"evidence": rowdict(row), "message": "证据更新成功"}
    finally:
        conn.close()


@router.delete("/api/evidence/{evidence_id}")
def delete_evidence(evidence_id: int):
    """删除证据事项（CASCADE 删除关联的标注和关系）"""
    conn = connect()
    try:
        # 验证证据存在并获取 case_id
        evidence = conn.execute(
            "SELECT id, title, case_id FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if not evidence:
            raise HTTPException(404, "证据不存在")

        # 获取关联统计（用于返回信息）
        annotations_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM evidence_annotations WHERE evidence_id = ?",
            (evidence_id,)
        ).fetchone()["cnt"]

        relations_count = conn.execute(
            """SELECT COUNT(*) as cnt FROM evidence_relations
               WHERE from_evidence_id = ? OR to_evidence_id = ?""",
            (evidence_id, evidence_id)
        ).fetchone()["cnt"]

        # 删除证据（CASCADE 会自动删除关联的 annotations 和 relations）
        conn.execute("DELETE FROM evidence WHERE id = ?", (evidence_id,))

        # 记录审计日志
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '删除证据', ?, ?)",
            (
                evidence["case_id"],
                f"证据ID:{evidence_id} 标题:《{evidence['title']}》 关联标注:{annotations_count} 关系:{relations_count}",
                now()
            )
        )

        conn.commit()

        return {
            "message": "证据删除成功",
            "deleted": {
                "evidence_id": evidence_id,
                "title": evidence["title"],
                "annotations_deleted": annotations_count,
                "relations_deleted": relations_count
            }
        }
    finally:
        conn.close()


def _validate_annotation_range(start: int | None, end: int | None, quote: str) -> None:
    if (start is None) != (end is None):
        raise HTTPException(400, "引用起止位置必须同时提供或同时为空")
    if start is not None and end is not None and (end <= start or end > len(quote)):
        raise HTTPException(400, "引用范围无效：结束位置须大于起始位置且不超过证据原文长度")


@router.get("/api/evidence/{evidence_id}/annotations")
def list_evidence_annotations(evidence_id: int):
    with transaction() as conn:
        evidence = conn.execute("SELECT case_id FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if evidence is None:
            raise HTTPException(404, "证据不存在")
        require_case_access(evidence["case_id"])
        return [dict(row) for row in conn.execute(
            "SELECT * FROM evidence_annotations WHERE evidence_id=? ORDER BY id", (evidence_id,),
        )]


@router.post("/api/evidence/{evidence_id}/annotations", status_code=201)
def create_evidence_annotation(evidence_id: int, body: AnnotationCreate):
    with transaction() as conn:
        evidence = conn.execute("SELECT case_id,quote FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if evidence is None:
            raise HTTPException(404, "证据不存在")
        require_case_access(evidence["case_id"])
        _validate_annotation_range(body.quote_start, body.quote_end, evidence["quote"] or "")
        annotation_id = conn.execute(
            """INSERT INTO evidence_annotations(evidence_id,user_name,annotation_type,content,quote_start,quote_end,status,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (evidence_id, actor_name(body.user_name), body.annotation_type, body.content,
             body.quote_start, body.quote_end, body.status, now()),
        ).lastrowid
        conn.execute(
            "INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'添加证据标注',?,?)",
            (evidence["case_id"], f"{actor_name(body.user_name)}：证据 {evidence_id} 标注 {annotation_id}", now()),
        )
        return dict(conn.execute("SELECT * FROM evidence_annotations WHERE id=?", (annotation_id,)).fetchone())


@router.patch("/api/evidence-annotations/{annotation_id}")
def update_evidence_annotation(annotation_id: int, body: AnnotationUpdate):
    with transaction() as conn:
        row = conn.execute(
            """SELECT a.*,e.case_id,e.quote FROM evidence_annotations a
               JOIN evidence e ON e.id=a.evidence_id WHERE a.id=?""", (annotation_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "标注不存在")
        require_case_access(row["case_id"])
        values = body.model_dump(exclude_unset=True)
        if not values:
            raise HTTPException(400, "至少需要更新一个字段")
        if any(value is None and key not in {"quote_start", "quote_end"} for key, value in values.items()):
            raise HTTPException(422, "标注类型、内容和状态不能为空")
        _validate_annotation_range(values.get("quote_start", row["quote_start"]),
                                   values.get("quote_end", row["quote_end"]), row["quote"] or "")
        assignments = ",".join(f"{key}=?" for key in values)
        conn.execute(f"UPDATE evidence_annotations SET {assignments} WHERE id=?", (*values.values(), annotation_id))
        conn.execute(
            "INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'更新证据标注',?,?)",
            (row["case_id"], f"{actor_name()}：标注 {annotation_id}", now()),
        )
        return dict(conn.execute("SELECT * FROM evidence_annotations WHERE id=?", (annotation_id,)).fetchone())


@router.delete("/api/evidence-annotations/{annotation_id}")
def delete_evidence_annotation(annotation_id: int):
    with transaction() as conn:
        row = conn.execute(
            "SELECT e.case_id FROM evidence_annotations a JOIN evidence e ON e.id=a.evidence_id WHERE a.id=?",
            (annotation_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "标注不存在")
        require_case_access(row["case_id"])
        conn.execute("DELETE FROM evidence_annotations WHERE id=?", (annotation_id,))
        conn.execute(
            "INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'删除证据标注',?,?)",
            (row["case_id"], f"{actor_name()}：标注 {annotation_id}", now()),
        )
        return {"message": "标注删除成功", "annotation_id": annotation_id}


@router.get("/api/cases/{case_id}/gap-analysis")
def get_gap_analysis(case_id: int, severity: str = Query(None)):
    """
    获取案件的证据疏漏检测结果

    参数:
        severity: 可选，按严重程度筛选 (高/中/低)
    """
    require_case(case_id)
    conn = connect()
    try:
        query = "SELECT * FROM gap_detections WHERE case_id = ?"
        params = [case_id]

        if severity:
            query += " AND severity = ?"
            params.append(severity)

        query += " ORDER BY CASE severity WHEN '高' THEN 1 WHEN '中' THEN 2 WHEN '低' THEN 3 END, id DESC"

        gaps = [rowdict(x) for x in conn.execute(query, params)]

        # 解析 affected_evidence_ids JSON 字段
        for gap in gaps:
            gap["affected_evidence_ids"] = json.loads(gap.get("affected_evidence_ids", "[]"))

        # 统计信息
        stats = {
            "total": len(gaps),
            "high": len([g for g in gaps if g["severity"] == "高"]),
            "medium": len([g for g in gaps if g["severity"] == "中"]),
            "low": len([g for g in gaps if g["severity"] == "低"]),
        }

        return {
            "gaps": gaps,
            "statistics": stats
        }
    finally:
        conn.close()


@router.get("/api/cases/{case_id}/gap-analysis/preview")
def preview_case_gap_analysis(case_id: int):
    require_case(case_id)
    try:
        return preview_gap_analysis(case_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/cases/{case_id}/gap-analysis/detect")
def detect_and_save_case_gaps(case_id: int):
    require_case(case_id)
    try:
        return save_gap_analysis(case_id, actor_name())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

