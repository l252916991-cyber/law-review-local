"""Case export packages: templates, rendered blocks and CSV formula neutralisation."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from app.core.files import safe_filename
from app.storage.db import BUILTIN_EXPORT_TEMPLATES, connect, rowdict


def csv_safe_cell(value: Any) -> Any:
    """Neutralize spreadsheet formulas without changing stored evidence.

    CSV quoting alone does not stop formula evaluation. Ignore leading Unicode
    whitespace/BOM when checking triggers, but preserve the original cell text.
    """
    if isinstance(value, str):
        probe = value
        while probe and (probe[0].isspace() or probe[0] == "\ufeff"):
            probe = probe[1:]
        if probe.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
    return value


EXPORT_RENDERER_VERSION = "export-blocks-v1"
DEFAULT_EXPORT_BLOCKS = BUILTIN_EXPORT_TEMPLATES[0]["blocks"]
_TEXT_BLOCK_EXT = {"case_summary": ".md", "qa_log": ".md", "static_markdown": ".md", "catalog_csv": ".csv", "evidence_table": ".csv"}


def _export_evidence_rows(evidence: list[dict[str, Any]], status: str | None) -> list[dict[str, Any]]:
    if not status:
        return evidence
    return [item for item in evidence if item["status"] == status]


def _evidence_table_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["证据事项", "类别", "待证事实", "可信度", "来源文件", "页码", "原文", "状态"])
    for item in rows:
        writer.writerow(
            [csv_safe_cell(value) for value in (item["title"], item["category"], item["fact"], item["credibility"], item.get("source_name", ""), f"{item['source_page_start']}-{item['source_page_end']}", item["quote"], item["status"])]
        )
    return buffer.getvalue()


def _block_filename(block: dict[str, Any]) -> str:
    explicit = block.get("filename")
    if explicit:
        return safe_filename(explicit)
    return safe_filename(block.get("title") or block["type"]) + _TEXT_BLOCK_EXT.get(block["type"], "")


def get_export_template(template_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM export_templates WHERE id = ?", (template_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    template = rowdict(row)
    template["blocks"] = json.loads(template["blocks_json"])
    template["builtin"] = bool(template["builtin"])
    return template


def build_export_package(
    case_id: int, template_id: int | None = None, *, final: bool = False, actor: str = "",
    report_id: int | None = None,
) -> tuple[Path, str]:
    """Preview or formal delivery of one case.

    A preview is rendered now and is explicitly ``finalized=false``. A formal
    delivery reads the frozen body of one approved report version and refuses
    to substitute another template or re-render from live tables.
    """
    from app.cases import report_delivery

    if not final:
        if report_id is not None:
            raise ValueError("只有正式交付可以指定报告版本")
        return report_delivery.build_preview_package(case_id, template_id, actor=actor)
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM cases WHERE id = ?", (case_id,)).fetchone():
            raise ValueError("案件不存在")
        if report_id is None:
            report = conn.execute(
                "SELECT * FROM case_reports WHERE case_id = ? AND status = '已批准' ORDER BY version DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        else:
            report = conn.execute(
                "SELECT * FROM case_reports WHERE id = ? AND case_id = ?", (report_id, case_id)
            ).fetchone()
    finally:
        conn.close()
    if report is None:
        if report_id is not None:
            raise ValueError("指定的报告不存在或不属于本案")
        # Template completeness is reported before the approval gate: authors
        # need to see what the selected template still lacks.
        missing = report_delivery.required_missing(case_id, template_id)
        if missing:
            raise ValueError("结案包缺少必选内容：" + "、".join(missing))
        raise ValueError("正式交付需要先批准一份报告：请在“成果导出”中提交复核并批准")
    if report["status"] != "已批准":
        raise ValueError("只有已批准的报告可以正式交付")
    if not report["approved_version_id"]:
        raise ValueError("该报告批准时没有冻结交付正文，请重新准备并提交复核后批准")
    if template_id is not None and template_id != report["template_id"]:
        raise ValueError("正式交付只能使用报告冻结的模板，不能替换正文")
    return report_delivery.build_frozen_package(case_id, report, actor=actor)


def build_export(case_id: int) -> Path:
    """Back-compatible wrapper: default layout, no final gating."""
    return build_export_package(case_id)[0]
