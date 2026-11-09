"""One immutable delivery body per report submission.

An approval is only meaningful for the exact bytes the reviewer saw, so a
submission freezes the rendered body — template blocks including static text,
case summary, catalog, evidence table, QA log and the full transaction sheet —
into a protected artifact directory and binds the approval to that version's
content hash. Formal export copies those frozen bytes back out and never
re-renders from live tables, so a concurrent edit can only produce "complete
frozen version" or "refused, prepare a new version".

Published summary and approval metadata are hashed separately: the body hash
covers rendered files and attachment digests, the source hash covers the case,
material, evidence, QA, transaction and template inputs. Approval time, approver
and the final ZIP hash stay outside both to avoid a self-referential digest.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import uuid
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from app.cases.exports import (
    DEFAULT_EXPORT_BLOCKS,
    EXPORT_RENDERER_VERSION,
    _block_filename,
    _evidence_table_csv,
    _export_evidence_rows,
    csv_safe_cell,
    get_export_template,
)
from app.core.digest import json_sha256
from app.core.files import contained_path, file_sha256, safe_filename
from app.documents.bank_ledger import iter_bank_transactions
from app.storage.db import SCHEMA_VERSION, connect, get_db_path, now, rowdict, transaction

ARTIFACT_ROOT = "report_versions"
TRANSACTION_SHEET = "资金流水.csv"
TRANSACTION_COLUMNS = ("transaction_time", "account", "direction", "amount_minor", "currency",
                       "counterparty", "memo", "source_row_number", "parse_status")


def artifact_root() -> Path:
    return get_db_path().parent / ARTIFACT_ROOT


# --- Source snapshot -------------------------------------------------------

def template_blocks(template: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize the block list so hashing does not depend on input shape.

    ``required`` is a promise made by a named template. An ad-hoc report with no
    template uses the same layout as a convenience, but it does not inherit a
    completeness promise nobody selected.
    """
    raw = (template or {}).get("blocks") or DEFAULT_EXPORT_BLOCKS
    blocks = []
    for block in raw:
        blocks.append({
            "type": block["type"],
            "title": block.get("title") or block["type"],
            "filename": block.get("filename"),
            "content": block.get("content") or "",
            "evidence_status": block.get("evidence_status"),
            "required": bool(block.get("required")) if template is not None else False,
        })
    return blocks


def _case_view(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "case_no": row["case_no"],
        "case_type": row["case_type"],
        "client_name": row["client_name"],
        "status": row["status"],
        "description": row["description"],
    }


def _document_view(row) -> dict[str, Any]:
    item = rowdict(row)
    stored = item.get("stored_path") or ""
    return {
        "id": item["id"],
        "name": item["name"],
        "doc_type": item.get("doc_type") or "",
        "pages": item.get("pages") or 0,
        "people": item.get("people") or "",
        "date_range": item.get("date_range") or "",
        "summary": item.get("summary") or "",
        "status": item.get("status") or "",
        "content_hash": item.get("content_hash") or "",
        "version": item.get("version") or 1,
        # Whether the bytes are present now; the absolute path stays out of the
        # hash so a restore to a new directory keeps approvals valid.
        "has_file": bool(stored) and Path(stored).is_file(),
    }


def _evidence_view(row) -> dict[str, Any]:
    item = rowdict(row)
    return {
        "id": item["id"],
        "title": item["title"],
        "category": item.get("category") or "",
        "fact": item.get("fact") or "",
        "credibility": item.get("credibility") or "",
        "quote": item.get("quote") or "",
        "status": item.get("status") or "",
        "source_document_id": item.get("source_document_id"),
        "source_name": item.get("source_name") or "",
        "source_page_start": item.get("source_page_start"),
        "source_page_end": item.get("source_page_end"),
        "approved_by": item.get("approved_by"),
        "approved_at": item.get("approved_at"),
        "version": item.get("version") or 1,
    }


def _message_view(row) -> dict[str, Any]:
    item = rowdict(row)
    return {
        "id": item["id"],
        "role": item.get("role") or "",
        "content": item.get("content") or "",
        "conversation_title": item.get("conversation_title") or "",
        "user_name": item.get("user_name") or "",
    }


def collect_source(conn, case_id: int, template: dict[str, Any] | None) -> dict[str, Any]:
    """Read every input the body shows, once, in one consistent snapshot.

    The QA log is only part of the source when the template selects it, so a
    report without that block does not depend on unrelated chat.
    """
    case_row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if case_row is None:
        raise ValueError("案件不存在")
    blocks = template_blocks(template)
    documents = [_document_view(row) for row in conn.execute(
        "SELECT * FROM documents WHERE case_id = ? ORDER BY id", (case_id,)
    )]
    evidence = [_evidence_view(row) for row in conn.execute(
        """SELECT e.*, d.name AS source_name FROM evidence e
           LEFT JOIN documents d ON d.id = e.source_document_id
           WHERE e.case_id = ? ORDER BY e.id""",
        (case_id,),
    )]
    messages: list[dict[str, Any]] = []
    if any(block["type"] == "qa_log" for block in blocks):
        messages = [_message_view(row) for row in conn.execute(
            """SELECT m.*, c.title AS conversation_title, c.user_name FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE c.case_id = ? ORDER BY m.id""",
            (case_id,),
        )]
    return {
        "renderer_version": EXPORT_RENDERER_VERSION,
        "case": _case_view(case_row),
        "documents": documents,
        "evidence": evidence,
        "messages": messages,
        "template": {
            "id": template["id"] if template else None,
            "name": template["name"] if template else "默认结案包",
            "builtin": bool(template["builtin"]) if template else True,
            "blocks": blocks,
        },
        "counts": {
            "documents": len(documents),
            "pages": sum(item["pages"] for item in documents),
            "evidence": len(evidence),
            "confirmed": sum(1 for item in evidence if item["status"] == "已确认"),
            "messages": len(messages),
        },
    }


def _hashed_source(source: dict[str, Any]) -> dict[str, Any]:
    """The inputs an approval covers. Display-only fields are excluded."""
    return {
        "renderer_version": source["renderer_version"],
        "case": source["case"],
        "documents": source["documents"],
        "evidence": source["evidence"],
        "messages": source["messages"],
        "template": {"id": source["template"]["id"], "blocks": source["template"]["blocks"]},
    }


def source_digest(source: dict[str, Any]) -> str:
    return json_sha256(_hashed_source(source))


def content_digest(body: dict[str, Any]) -> str:
    return json_sha256({
        "blocks": body["blocks"],
        "transactions": body["transactions"],
        "attachments": body["attachments"],
    })


# --- Rendering -------------------------------------------------------------

def _summary_text(source: dict[str, Any]) -> str:
    case = source["case"]
    return (
        f"# {case['title']}\n\n"
        f"- 案号：{case['case_no']}\n- 类型：{case['case_type']}\n- 当事人：{case['client_name']}\n"
        f"- 状态：{case['status']}\n- 卷宗：{len(source['documents'])}份\n"
        f"- 证据事项：{len(source['evidence'])}条\n\n"
        f"## 案件说明\n\n{case['description']}\n\n"
        "> 本文件由本地复刻系统生成，所有AI结果均需执业律师复核。\n"
    )


def _catalog_csv(source: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["文件名", "文书类型", "页数", "涉及人员", "时间范围", "摘要", "状态"])
    for doc in source["documents"]:
        writer.writerow([csv_safe_cell(doc[key]) for key in
                         ("name", "doc_type", "pages", "people", "date_range", "summary", "status")])
    return "\ufeff" + buffer.getvalue()


def _qa_log(source: dict[str, Any]) -> str:
    chats = ["# 阅卷问答记录\n"]
    for message in source["messages"]:
        chats.append(
            f"## {message['conversation_title']} · {message['user_name']}\n\n"
            f"**{message['role']}**\n\n{message['content']}\n"
        )
    if not source["messages"]:
        chats.append("\n[待补充：暂无问答记录]")
    return "\n".join(chats)


def _evidence_csv(source: dict[str, Any], status: str | None) -> str:
    rows = [
        {"title": item["title"], "category": item["category"], "fact": item["fact"],
         "credibility": item["credibility"], "source_name": item["source_name"],
         "source_page_start": item["source_page_start"], "source_page_end": item["source_page_end"],
         "quote": item["quote"], "status": item["status"]}
        for item in _export_evidence_rows(source["evidence"], status)
    ]
    return "\ufeff" + _evidence_table_csv(rows)


def render_block(block: dict[str, Any], source: dict[str, Any]) -> str:
    kind = block["type"]
    if kind == "case_summary":
        return _summary_text(source)
    if kind == "catalog_csv":
        return _catalog_csv(source)
    if kind == "evidence_table":
        return _evidence_csv(source, block.get("evidence_status"))
    if kind == "qa_log":
        return _qa_log(source)
    if kind == "static_markdown":
        return block.get("content") or "[待补充：模板未填写内容]"
    raise ValueError(f"导出区块类型不受支持：{kind}")


def render_text_files(source: dict[str, Any]) -> list[dict[str, Any]]:
    files = []
    for block in source["template"]["blocks"]:
        if block["type"] == "attachments":
            continue
        files.append({
            "type": block["type"],
            "title": block["title"],
            "archive_name": _block_filename(block),
            "text": render_block(block, source),
        })
    return files


def block_has_data(block: dict[str, Any], source: dict[str, Any]) -> bool:
    kind = block["type"]
    if kind in {"case_summary", "static_markdown"}:
        return True
    if kind == "catalog_csv":
        return bool(source["documents"])
    if kind == "evidence_table":
        return bool(_export_evidence_rows(source["evidence"], block.get("evidence_status")))
    if kind == "qa_log":
        return bool(source["messages"])
    if kind == "attachments":
        return any(item["has_file"] for item in source["documents"])
    return False


def missing_required_blocks(source: dict[str, Any]) -> list[str]:
    return [block["title"] for block in source["template"]["blocks"]
            if block["required"] and not block_has_data(block, source)]


def transaction_sheet(case_id: int, *, include_text: bool = True) -> dict[str, Any]:
    """Stream the whole sheet so a case with more rows than one page still freezes."""
    hasher = hashlib.sha256()
    hasher.update("\ufeff".encode("utf-8"))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["日期", "账号", "方向", "金额（分）", "币种", "对方", "摘要", "来源行", "状态"])
    chunks: list[str] = []
    count = 0
    currencies: list[str] = []
    rows = iter_bank_transactions(case_id)
    try:
        for row in rows:
            writer.writerow([csv_safe_cell(row[key]) for key in TRANSACTION_COLUMNS])
            count += 1
            currency = row.get("currency") or "CNY"
            if currency not in currencies:
                currencies.append(currency)
            if buffer.tell() and count % 500 == 0:
                chunk = buffer.getvalue()
                hasher.update(chunk.encode("utf-8"))
                if include_text:
                    chunks.append(chunk)
                buffer.seek(0)
                buffer.truncate(0)
    finally:
        rows.close()
    chunk = buffer.getvalue()
    hasher.update(chunk.encode("utf-8"))
    result = {
        "archive_name": TRANSACTION_SHEET,
        "count": count,
        "currencies": currencies,
        "sha256": hasher.hexdigest(),
    }
    if include_text:
        chunks.append(chunk)
        result["text"] = "\ufeff" + "".join(chunks)
    return result


def stored_paths(case_id: int) -> dict[int, str]:
    """Current controlled path per document; never part of a frozen hash.

    A restore rewrites ``documents.stored_path`` to a new directory, so the
    frozen body identifies attachments by document id and content digest only.
    """
    conn = connect()
    try:
        return {row["id"]: row["stored_path"] or "" for row in conn.execute(
            "SELECT id, stored_path FROM documents WHERE case_id = ?", (case_id,)
        )}
    finally:
        conn.close()


def _attachment_specs(source: dict[str, Any], data_dir: Path, *, strict: bool) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    folders = [block for block in source["template"]["blocks"] if block["type"] == "attachments"]
    if not folders:
        return specs
    paths = stored_paths(source["case"]["id"])
    for block in folders:
        folder = safe_filename(block.get("filename") or block.get("title") or "原始卷宗")
        for doc in source["documents"]:
            stored = paths.get(doc["id"], "")
            if not stored:
                # Metadata-only rows carry no file; there is nothing to package.
                continue
            path = Path(stored)
            if not (path.exists() and path.is_file()):
                if strict:
                    raise ValueError(f"附件缺失：《{doc['name']}》，请补齐后再提交复核")
                continue
            path = contained_path(path, data_dir)
            specs.append({
                "document_id": doc["id"],
                "archive_name": f"{folder}/{safe_filename(doc['name'])}",
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            })
    return specs


def attachment_path(paths: dict[int, str], attachment: dict[str, Any]) -> Path:
    """Resolve one frozen attachment against the current controlled paths."""
    stored = paths.get(attachment["document_id"]) or ""
    if not stored:
        raise ValueError(f"附件已从案件移除：《{attachment['archive_name']}》")
    path = Path(stored)
    if not path.is_file():
        raise ValueError(f"附件缺失：《{attachment['archive_name']}》")
    return contained_path(path, get_db_path().parent)


# --- Freezing --------------------------------------------------------------

def resolve_artifact_dir(relative: str) -> Path:
    root = artifact_root().resolve()
    path = (get_db_path().parent / relative).resolve() if relative else root
    if path != root and root not in path.parents:
        raise ValueError("冻结正文目录超出数据目录")
    return path


def _write_file(path: Path, data: bytes) -> dict[str, Any]:
    path.write_bytes(data)
    return {"file": path.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def freeze_body(case_id: int, report_id: int, seq: int, source: dict[str, Any],
                sheet: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """Write the rendered body into a new artifact directory.

    The directory is built under a temporary name and renamed last, so a failed
    or interrupted write never leaves a directory that looks complete. The
    parent transaction decides whether the report may move to 待复核.
    """
    data_dir = get_db_path().parent
    sheet = sheet or transaction_sheet(case_id)
    blocks: list[dict[str, Any]] = []
    attachments = _attachment_specs(source, data_dir, strict=True)
    directory = artifact_root() / f"case-{case_id}" / f"report-{report_id}-{seq}-{uuid.uuid4().hex[:8]}"
    temporary = directory.with_name(directory.name + ".writing")
    try:
        temporary.mkdir(parents=True)
        for index, item in enumerate(render_text_files(source), start=1):
            written = _write_file(temporary / f"{index:03d}-{safe_filename(item['archive_name'])}",
                                  item["text"].encode("utf-8"))
            blocks.append({
                "type": item["type"],
                "title": item["title"],
                "archive_name": item["archive_name"],
                **written,
            })
        sheet_entry = _write_file(temporary / f"transactions-{safe_filename(sheet['archive_name'])}",
                                  sheet["text"].encode("utf-8"))
        transactions = {
            "archive_name": sheet["archive_name"],
            "count": sheet["count"],
            "currencies": sheet["currencies"],
            "sha256": sheet_entry["sha256"],
            "size": sheet_entry["size"],
            "file": sheet_entry["file"],
        }
        body = {"blocks": blocks, "transactions": transactions, "attachments": attachments}
        (temporary / "body.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, directory)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return body, str(directory.relative_to(data_dir))


def load_body(version_row) -> dict[str, Any]:
    try:
        body = json.loads(version_row["content_json"])
    except ValueError as exc:
        raise ValueError("冻结正文记录损坏，请重新提交复核") from exc
    if content_digest(body) != version_row["content_hash"]:
        raise ValueError("冻结正文摘要不一致，请重新提交复核")
    return body


def verify_body(version_row) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check the frozen files and attachment bytes before approval or delivery."""
    body = load_body(version_row)
    try:
        source = json.loads(version_row["source_json"])
    except ValueError as exc:
        raise ValueError("冻结来源记录损坏，请重新提交复核") from exc
    if source_digest(source) != version_row["source_hash"]:
        raise ValueError("冻结来源摘要不一致，请重新提交复核")
    directory = resolve_artifact_dir(version_row["artifact_dir"])
    entries = [*body["blocks"], body["transactions"]]
    for entry in entries:
        path = directory / entry["file"]
        if not path.is_file():
            raise ValueError(f"冻结正文文件缺失：{entry['archive_name']}")
        if path.stat().st_size != entry["size"] or file_sha256(path) != entry["sha256"]:
            raise ValueError(f"冻结正文文件已变化：{entry['archive_name']}")
    paths = stored_paths(source["case"]["id"])
    for attachment in body["attachments"]:
        path = attachment_path(paths, attachment)
        if path.stat().st_size != attachment["size"] or file_sha256(path) != attachment["sha256"]:
            raise ValueError(f"附件内容已变化：《{attachment['archive_name']}》")
    return body, source


# --- Package assembly ------------------------------------------------------

def _manifest(source: dict[str, Any], *, finalized: bool, report_info: dict[str, Any] | None,
              frozen: dict[str, Any] | None) -> dict[str, Any]:
    template = source["template"]
    return {
        "generated_at": now(),
        "schema_version": SCHEMA_VERSION,
        "renderer_version": source["renderer_version"],
        "finalized": bool(finalized),
        "case": {key: source["case"][key] for key in ("id", "title", "case_no", "status")},
        "template": {"id": template["id"], "name": template["name"], "builtin": template["builtin"],
                     "version": None, "blocks": template["blocks"]},
        "documents": [
            {"id": doc["id"], "name": doc["name"], "content_hash": doc["content_hash"] or None,
             "pages": doc["pages"], "doc_type": doc["doc_type"], "status": doc["status"]}
            for doc in source["documents"]
        ],
        "evidence": [
            {"id": item["id"], "title": item["title"], "status": item["status"],
             "approved_by": item["approved_by"], "approved_at": item["approved_at"],
             "source_document_id": item["source_document_id"],
             "source_pages": [item["source_page_start"], item["source_page_end"]]}
            for item in source["evidence"]
        ],
        "files": [],
        "artifacts": [],
        "report": report_info,
        "frozen": frozen,
    }


def _package_path(case_title: str) -> Path:
    export_dir = get_db_path().parent / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return export_dir / f"{safe_filename(case_title)}_{stamp}_{uuid.uuid4().hex[:8]}.zip"


def _write_package(path: Path, source: dict[str, Any], *, finalized: bool,
                   report_info: dict[str, Any] | None, frozen: dict[str, Any] | None,
                   blocks: list[dict[str, Any]], sheet: dict[str, Any],
                   attachments: list[dict[str, Any]]) -> str:
    manifest = _manifest(source, finalized=finalized, report_info=report_info, frozen=frozen)
    manifest["artifacts"] = [{
        "archive_name": sheet["archive_name"],
        "sha256": sheet["sha256"],
        "transaction_count": sheet["count"],
        "currencies": sheet["currencies"],
    }]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for block in blocks:
            archive.writestr(block["archive_name"], block["data"])
        archive.writestr(sheet["archive_name"], sheet["data"])
        for attachment in attachments:
            archive.write(attachment["path"], attachment["archive_name"])
            manifest["files"].append({
                "archive_name": attachment["archive_name"],
                "document_id": attachment["document_id"],
                "sha256": attachment["sha256"],
                "size": attachment["size"],
            })
        archive.writestr("清单.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return file_sha256(path)


def build_preview_package(case_id: int, template_id: int | None, *, actor: str = "",
                          template: dict[str, Any] | None = None) -> tuple[Path, str]:
    """Preview export: rendered now, finalized=False, no approval involved."""
    if template is None and template_id is not None:
        template = get_export_template(template_id)
        if template is None:
            raise ValueError("结案模板不存在")
    with closing(connect()) as conn:
        source = collect_source(conn, case_id, template)
    files = render_text_files(source)
    sheet = transaction_sheet(case_id)
    attachments = _attachment_specs(source, get_db_path().parent, strict=False)
    path = _package_path(source["case"]["title"])
    package_sha = _write_package(
        path, source, finalized=False, report_info=None, frozen=None,
        blocks=[{"archive_name": item["archive_name"], "data": item["text"].encode("utf-8")} for item in files],
        sheet={**sheet, "data": sheet["text"].encode("utf-8")},
        attachments=[{**item, "path": attachment_path(stored_paths(case_id), item)} for item in attachments],
    )
    with transaction() as conn:
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '导出结案包', ?, ?)",
            (case_id, path.name, now()),
        )
    return path, package_sha


def build_frozen_package(case_id: int, report_row, *, actor: str = "") -> tuple[Path, str]:
    """Formal delivery: the frozen bytes of the approved version, nothing live."""
    version_id = report_row["approved_version_id"]
    if not version_id:
        raise ValueError("该报告批准时没有冻结交付正文，请重新准备并提交复核后批准")
    with closing(connect()) as conn:
        version = conn.execute("SELECT * FROM case_report_versions WHERE id = ?", (version_id,)).fetchone()
    if version is None or version["report_id"] != report_row["id"]:
        raise ValueError("找不到该报告已批准的冻结正文，请重新提交复核")
    if version["status"] != "frozen":
        raise ValueError("最后一次冻结未完成，不能交付，请重新提交复核")
    body, source = verify_body(version)
    template = None
    if version["template_id"] is not None:
        template = get_export_template(version["template_id"])
        if template is None:
            raise ValueError("报告冻结的结案模板已被删除，请重新准备并批准")
    with closing(connect()) as conn:
        current = collect_source(conn, case_id, template)
    if source_digest(current) != version["source_hash"]:
        raise ValueError("报告依据的材料、证据、问答或模板已变化，请重新复核并批准后再导出")
    if sheet_stale(case_id, body):
        raise ValueError("报告冻结后的资金流水已变化，请重新复核并批准后再导出")
    report_info = {
        "id": report_row["id"],
        "version": report_row["version"],
        "title": report_row["title"],
        "status": report_row["status"],
        "basis_hash": report_row["basis_hash"],
        "counts": source["counts"],
        "prepared_by": report_row["prepared_by"],
        "reviewed_by": report_row["reviewed_by"],
        "approved_by": report_row["approved_by"],
        "approved_at": report_row["approved_at"],
        "frozen_version_id": version["id"],
        "frozen_version_seq": version["seq"],
        "content_hash": version["content_hash"],
        "source_hash": version["source_hash"],
        "renderer_version": version["renderer_version"],
    }
    directory = resolve_artifact_dir(version["artifact_dir"])
    blocks = [{"archive_name": item["archive_name"], "data": (directory / item["file"]).read_bytes()}
              for item in body["blocks"]]
    sheet = {
        "archive_name": body["transactions"]["archive_name"],
        "sha256": body["transactions"]["sha256"],
        "count": body["transactions"]["count"],
        "currencies": body["transactions"]["currencies"],
        "data": (directory / body["transactions"]["file"]).read_bytes(),
    }
    paths = stored_paths(case_id)
    attachments = [{**item, "path": attachment_path(paths, item)} for item in body["attachments"]]
    path = _package_path(source["case"]["title"])
    package_sha = _write_package(
        path, source, finalized=True, report_info=report_info,
        frozen={"version_id": version["id"], "seq": version["seq"],
                "content_hash": version["content_hash"], "source_hash": version["source_hash"],
                "renderer_version": version["renderer_version"], "frozen_at": version["frozen_at"]},
        blocks=blocks, sheet=sheet, attachments=attachments,
    )
    _register_delivery(case_id, report_row["id"], version, path, package_sha, actor)
    return path, package_sha


def _register_delivery(case_id: int, report_id: int, version, path: Path, package_sha: str, actor: str) -> None:
    """Re-check state and permissions, then record the delivery.

    A report voided, superseded, re-versioned or made stale while the ZIP was assembled, or
    an operator whose export right was withdrawn, must not be registered as a
    completed delivery. The bytes already produced are not claimed as recalled.
    """
    from app.core.security import current_principal
    principal = current_principal()
    with transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM case_reports WHERE id = ?", (report_id,)).fetchone()
        if (current is None or current["status"] != "已批准"
                or current["approved_version_id"] != version["id"]):
            raise ValueError("报告状态或批准版本在导出期间变化，已拒绝本次交付")
        if not (principal.admin or case_id in principal.case_ids):
            raise ValueError("案件授权在导出期间变化，已拒绝本次交付")
        if not principal.has_for_case("export", case_id):
            raise ValueError("导出权限在导出期间变化，已拒绝本次交付")
        # Packaging can take time, especially with attachments. Lock only this
        # final database check and registration, not ZIP assembly or file reads.
        # BEGIN IMMEDIATE keeps concurrent writers out between these checks and
        # the delivery event; ordinary WAL readers can continue. A page edit is
        # represented by its document version in the source snapshot.
        if current_source_digest(conn, case_id, version["template_id"]) != version["source_hash"]:
            raise ValueError("报告依据在导出期间已变化，已拒绝本次交付，请重新复核并批准")
        if sheet_stale(case_id, load_body(version)):
            raise ValueError("报告资金流水在导出期间已变化，已拒绝本次交付，请重新复核并批准")
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '导出结案包', ?, ?)",
            (case_id, path.name, now()),
        )
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '模板结案打包', ?, ?)",
            (case_id, f"报告 v{current['version']}#{report_id} 批准人[{current['approved_by']}] "
                      f"冻结版本#{version['id']} 正文摘要 {version['content_hash'][:16]} "
                      f"包哈希 {package_sha[:16]} 操作人[{actor or '未知'}]", now()),
        )
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '正式交付登记', ?, ?)",
            (case_id, f"报告#{report_id} v{current['version']} 版本#{version['id']} "
                      f"正文摘要 {version['content_hash']} 来源摘要 {version['source_hash']} "
                      f"包摘要 {package_sha} 文件 {path.name}", now()),
        )


def required_missing(case_id: int, template_id: int | None) -> list[str]:
    """Required blocks of the selected template that have no content yet."""
    template = None
    if template_id is not None:
        template = get_export_template(template_id)
        if template is None:
            raise ValueError("结案模板不存在")
    with closing(connect()) as conn:
        source = collect_source(conn, case_id, template)
    return missing_required_blocks(source)


def current_source(conn, case_id: int, template_id: int | None) -> dict[str, Any]:
    """The inputs as they are now, for staleness and completeness display."""
    template = None
    if template_id is not None:
        template = get_export_template(template_id)
        if template is None:
            raise ValueError("结案模板已不存在")
    return collect_source(conn, case_id, template)


def current_source_digest(conn, case_id: int, template_id: int | None) -> str:
    """Digest of the inputs as they are now, for staleness display and export."""
    return source_digest(current_source(conn, case_id, template_id))


def sheet_stale(case_id: int, body: dict[str, Any]) -> bool:
    """The transaction sheet is part of the delivery body, so it must not move.

    Comparing the frozen sheet digest with the current one avoids re-reading the
    whole sheet on every list call while still covering order, count and
    currency.
    """
    frozen = body.get("transactions") or {}
    current = transaction_sheet(case_id, include_text=False)
    return (current["sha256"] != frozen.get("sha256")
            or current["count"] != frozen.get("count"))


def freeze_for_submission(conn, report_row, *, actor: str) -> dict[str, Any]:
    """Freeze the body of the report about to be submitted for review."""
    template = None
    if report_row["template_id"] is not None:
        template = get_export_template(report_row["template_id"])
        if template is None:
            raise ValueError("报告引用的结案模板已不存在，请重新准备报告")
    source = collect_source(conn, report_row["case_id"], template)
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM case_report_versions WHERE report_id = ?", (report_row["id"],)
    ).fetchone()[0]
    body, relative_dir = freeze_body(report_row["case_id"], report_row["id"], seq, source)
    source_hash = source_digest(source)
    content_hash = content_digest(body)
    version_id = conn.execute(
        "INSERT INTO case_report_versions(report_id, seq, report_version, template_id, renderer_version, "
        "source_json, source_hash, content_json, content_hash, artifact_dir, status, frozen_by, frozen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'frozen',?,?)",
        (report_row["id"], seq, report_row["version"], report_row["template_id"], source["renderer_version"],
         json.dumps(source, ensure_ascii=False, sort_keys=True), source_hash,
         json.dumps(body, ensure_ascii=False, sort_keys=True), content_hash,
         relative_dir, actor, now()),
    ).lastrowid
    return {
        "id": version_id, "seq": seq, "artifact_dir": relative_dir,
        "content_hash": content_hash, "source_hash": source_hash,
        "source": source, "body": body,
    }
