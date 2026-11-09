"""Evidence gap analysis and rule-based candidate evidence extraction for a case."""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.text import concise
from app.storage.db import connect, now, rowdict, transaction

MAX_GAP_ANALYSIS_PAGES = 2000


def _case_pages_for_gap_analysis(case_id: int) -> list[dict[str, Any]]:
    """Load the complete bounded case page set; never silently sample it."""
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.case_id=?",
            (case_id,),
        ).fetchone()[0]
        if count > MAX_GAP_ANALYSIS_PAGES:
            raise ValueError(f"案件页数超过疏漏检测上限 {MAX_GAP_ANALYSIS_PAGES}")
        rows = conn.execute(
            """SELECT p.id AS page_id,p.page_no,p.text,p.summary,d.id AS document_id,
                      d.name,d.doc_type,d.people,d.date_range
               FROM pages p JOIN documents d ON d.id=p.document_id
               WHERE d.case_id=? ORDER BY d.id,p.page_no""",
            (case_id,),
        ).fetchall()
    finally:
        conn.close()
    contexts = [rowdict(row) for row in rows]
    for item in contexts:
        item["quote"] = concise(item.get("text", ""), 800)
    return contexts


def preview_gap_analysis(case_id: int) -> dict[str, Any]:
    """Run deterministic gap heuristics over every case page without persisting."""
    from app.review.agents import GapDetectionAgent

    contexts = _case_pages_for_gap_analysis(case_id)
    result = GapDetectionAgent(case_id).run("对整个案件执行疏漏检测", contexts)
    return {
        **result,
        "persisted": False,
        "page_count": len(contexts),
        "scope": "complete_case_pages",
        "semantic_entailment_checked": False,
        "lawyer_review_required": True,
    }


def save_gap_analysis(case_id: int, user_name: str) -> dict[str, Any]:
    """Detect and idempotently save new gap candidates for explicit review."""
    preview = preview_gap_analysis(case_id)
    inserted = 0
    duplicates = 0
    with transaction() as conn:
        for gap in preview["gaps"]:
            affected = json.dumps(gap.get("affected_evidence_ids", []), ensure_ascii=False)
            exists = conn.execute(
                """SELECT 1 FROM gap_detections
                   WHERE case_id=? AND gap_type=? AND severity=? AND description=?
                     AND suggestion=? AND affected_evidence_ids=? LIMIT 1""",
                (case_id, gap["type"], gap["severity"], gap["description"], gap["suggestion"], affected),
            ).fetchone()
            if exists:
                duplicates += 1
                continue
            conn.execute(
                """INSERT INTO gap_detections(
                       case_id,run_id,gap_type,severity,description,suggestion,affected_evidence_ids,created_at
                   ) VALUES (?,NULL,?,?,?,?,?,?)""",
                (case_id, gap["type"], gap["severity"], gap["description"], gap["suggestion"], affected, now()),
            )
            inserted += 1
        conn.execute(
            "INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'保存证据疏漏检测',?,?)",
            (case_id, f"{user_name or '本机律师'}：新增{inserted}项，去重{duplicates}项", now()),
        )
    return {**preview, "persisted": True, "inserted": inserted, "duplicates": duplicates}


def auto_analyze_case(case_id: int) -> dict[str, int]:
    conn = connect()
    try:
        pages = conn.execute(
            """
            SELECT p.*, d.name, d.doc_type FROM pages p JOIN documents d ON d.id = p.document_id
            WHERE d.case_id = ? ORDER BY d.id, p.page_no
            """,
            (case_id,),
        ).fetchall()
        existing = {x[0] for x in conn.execute("SELECT title FROM evidence WHERE case_id = ?", (case_id,))}
    finally:
        conn.close()
    candidates: list[tuple[str, str, str, Any, str]] = []
    rules = [
        (r"不知道|不清楚|没听说|我以为", "口供中的不知情/认识表述", "言词证据", "待质证"),
        (r"元|转账|入账|流水|账户", "资金流向事项", "银行流水", "待核验"),
        (r"邮件|聊天|回复|宣传", "电子沟通与宣传事项", "电子数据", "待复核"),
        (r"矛盾|不一致|但未|未见", "潜在矛盾或缺失事项", "审查事项", "待复核"),
    ]
    for page in pages:
        for pattern, title, category, status in rules:
            if re.search(pattern, page["text"]) and title not in existing and all(x[0] != title for x in candidates):
                candidates.append((title, category, concise(page["text"], 140), page, status))
    ts = now()
    created = 0
    with transaction() as conn:
        for title, category, fact, page, status in candidates:
            conn.execute(
                """
                INSERT INTO evidence(case_id, title, category, fact, credibility, source_document_id,
                                     source_page_start, source_page_end, quote, status, created_at)
                VALUES (?, ?, ?, ?, '待核验', ?, ?, ?, ?, ?, ?)
                """,
                (case_id, title, category, fact, page["document_id"], page["page_no"], page["page_no"], concise(page["text"], 180), status, ts),
            )
            created += 1
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '运行证据分析', ?, ?)",
            (case_id, f"新增{created}条待复核事项", ts),
        )
    return {"created": created, "scanned_pages": len(pages)}
