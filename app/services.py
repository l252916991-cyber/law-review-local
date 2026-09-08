from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextvars import ContextVar
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument

from .db import BUILTIN_EXPORT_TEMPLATES, SCHEMA_VERSION, connect, get_db_path, now, transaction
from .config import LLMConfig
from .bank_transactions import PARSER_VERSION, parse_csv, parse_spreadsheet


class ArchivedConversationError(ValueError):
    """Raised when a read-only archived conversation is used for new chat."""


LOCAL_LLM_URL = LLMConfig.from_env().base_url
LOCAL_LLM_MODEL = LLMConfig.from_env().model
logger = logging.getLogger(__name__)

SEMANTIC_GROUPS = [
    ["不知道", "不清楚", "不了解", "没听说", "不知情", "我以为合法", "认为是合法的"],
    ["固定回报", "保本保息", "年化收益", "承诺收益", "到期还本"],
    ["公开募集", "公开宣传", "不特定对象", "二维码转发", "社会公众"],
    ["资金流向", "银行流水", "转账", "入账", "出账", "账户"],
]

STOP_CHARS = set("的了是在与和或对把被从到这那有无是否什么哪些怎么为什么请帮我分析说明情况问题关于进行一个")


def rowdict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def safe_filename(name: str, max_len: int = 180) -> str:
    """
    清理文件名，移除危险字符

    防御层级：
    1. 提取基本文件名（防止路径穿越）
    2. 替换危险字符
    3. 处理反斜杠（Windows 路径分隔符）
    4. 移除连续点号（防御纵深）
    5. 长度截断
    """
    # 提取文件名（Path.name 自动处理 / 和 \）
    name = Path(name).name.strip() or "未命名文件"

    # 清理危险字符（包括反斜杠）
    name = re.sub(r"[\x00-\x1f/:*?\"<>|\\]", "_", name)

    # 移除连续点号（防御纵深）
    name = re.sub(r"\.\.+", ".", name)

    # 移除开头的点和下划线
    name = re.sub(r"^[._]+", "", name) or "unnamed"

    return name[:max_len]


MAX_PARSE_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_TEXT_CHARS = 5_000_000


def contained_path(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    base = root.resolve(strict=True)
    if resolved != base and base not in resolved.parents:
        raise ValueError("文件路径超出案件目录")
    return resolved


def _check_parse_budget(path: Path) -> None:
    if path.stat().st_size > MAX_PARSE_BYTES:
        raise ValueError("文件超过解析大小限制")


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
REMOTE_MODEL_APPROVAL_ENV = "LAW_REVIEW_ALLOW_REMOTE_MODELS"


def assert_model_endpoint_allowed(base_url: str) -> None:
    """Case text must not leave this machine without explicit deployment approval.

    Loopback model endpoints are always allowed. Any other destination requires
    LAW_REVIEW_ALLOW_REMOTE_MODELS=1 and https, so an operator cannot silently
    point case data at an unapproved remote service.
    """
    parsed = urllib.parse.urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if not parsed.scheme or not host:
        raise ValueError("模型服务地址无效")
    if host in LOOPBACK_HOSTS and parsed.scheme in {"http", "https"}:
        return
    if os.getenv(REMOTE_MODEL_APPROVAL_ENV) != "1":
        raise ValueError("外发模型服务未获批准：仅允许本机回环地址，或显式配置 LAW_REVIEW_ALLOW_REMOTE_MODELS=1")
    if parsed.scheme != "https":
        raise ValueError("远端模型服务必须使用 https")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a server cannot relay request bodies elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def egress_opener() -> urllib.request.OpenerDirector:
    """Opener with system proxies disabled and redirects refused."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def run_text_command(args: list[str], timeout: int = 120) -> str:
    proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    if proc.returncode:
        raise RuntimeError(f"{Path(args[0]).name} exited with code {proc.returncode}")
    return proc.stdout.decode("utf-8", errors="ignore")


def upload_error_message(exc: Exception) -> str:
    """Return useful parser diagnostics without filesystem paths or file text."""
    if isinstance(exc, ValueError) and str(exc).startswith("暂不支持该文件类型"):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return "缺少对应的本机文档解析工具，请运行 scripts/doctor.py 检查环境"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "文档解析超时；请拆分文件或检查 OCR 资源"
    return f"文档解析失败（{type(exc).__name__}）；详细原因仅记录在本机诊断日志"


def _persist_parsed_bank_rows(case_id: int, document_id: int, source_hash: str, rows: list[Any]) -> dict[str, Any]:
    with transaction() as conn:
        document = conn.execute("SELECT case_id FROM documents WHERE id=?", (document_id,)).fetchone()
        if not document or document["case_id"] != case_id:
            raise ValueError("流水来源文档不属于当前案件")
        for item in rows:
            conn.execute(
                """INSERT OR IGNORE INTO bank_transactions(case_id,source_document_id,source_document_hash,source_sheet,source_row_number,source_ref_json,account,direction,amount_minor,currency,amount_raw,transaction_time,time_raw,counterparty,memo,raw_row_json,parse_status,parse_warnings_json,parser_version,row_fingerprint,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (case_id, document_id, source_hash, item.source_sheet, item.source_row_number, json.dumps({"document_id": document_id, "sheet": item.source_sheet, "row": item.source_row_number}, ensure_ascii=False), item.account, item.direction, item.amount_minor, item.currency, item.amount_raw, item.transaction_time, item.time_raw, item.counterparty, item.memo, item.raw_row_json, item.parse_status, json.dumps(item.warnings, ensure_ascii=False), PARSER_VERSION, item.row_fingerprint, now()),
            )
    return {"rows": len(rows), "parsed": sum(x.parse_status == "parsed" for x in rows), "needs_review": sum(x.parse_status == "needs_review" for x in rows), "parser_version": PARSER_VERSION}


def persist_bank_transactions(case_id: int, document_id: int, source_hash: str, payload: bytes, *, sheet: str = "") -> dict[str, Any]:
    """Parse and idempotently persist explicit bank CSV rows for one document."""
    return _persist_parsed_bank_rows(case_id, document_id, source_hash, parse_csv(payload, sheet=sheet))


def persist_parsed_bank_rows(case_id: int, document_id: int, source_hash: str, rows: list[Any]) -> dict[str, Any]:
    """Persist already parsed spreadsheet rows through the CSV-equivalent contract."""
    return _persist_parsed_bank_rows(case_id, document_id, source_hash, rows)


def list_bank_transactions(case_id: int, *, limit: int = 100, offset: int = 0, account: str | None = None, direction: str | None = None, counterparty: str | None = None, date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    limit = max(1, min(limit, 500)); offset = max(0, offset)
    clauses = ["case_id=?"]; params: list[Any] = [case_id]
    for field, value in (("account", account), ("counterparty", counterparty)):
        if value: clauses.append(f"{field} LIKE ?"); params.append(f"%{value}%")
    if direction: clauses.append("direction=?"); params.append(direction)
    if date_from: clauses.append("transaction_time>=?"); params.append(date_from)
    if date_to: clauses.append("transaction_time<=?"); params.append(date_to)
    where = " AND ".join(clauses); conn = connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM bank_transactions WHERE {where}", params).fetchone()[0]
        rows = [rowdict(row) for row in conn.execute(f"SELECT * FROM bank_transactions WHERE {where} ORDER BY transaction_time, id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()]
    finally: conn.close()
    return {"transactions": rows, "pagination": {"total": total, "limit": limit, "offset": offset}}


def summarize_bank_transactions(case_id: int, **filters: Any) -> dict[str, Any]:
    rows = list_bank_transactions(case_id, limit=500, offset=0, **filters)["transactions"]
    inflow = [r for r in rows if r["direction"] == "inflow" and r["amount_minor"] is not None]; outflow = [r for r in rows if r["direction"] == "outflow" and r["amount_minor"] is not None]
    def group(key: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in rows:
            if row["amount_minor"] is not None: result[row[key] or "(空)"] = result.get(row[key] or "(空)", 0) + row["amount_minor"]
        return result
    by_period: dict[str, int] = {}
    for row in rows:
        if row["amount_minor"] is not None and row["transaction_time"]: by_period[row["transaction_time"][:7]] = by_period.get(row["transaction_time"][:7], 0) + row["amount_minor"]
    return {"transaction_count": len(rows), "inflow_count": len(inflow), "outflow_count": len(outflow), "inflow_minor": sum(r["amount_minor"] for r in inflow), "outflow_minor": sum(r["amount_minor"] for r in outflow), "net_minor": sum(r["amount_minor"] for r in inflow) - sum(r["amount_minor"] for r in outflow), "currency": rows[0]["currency"] if rows else "CNY", "by_account": group("account"), "by_counterparty": group("counterparty"), "by_period": by_period, "filters": filters}


def bank_transaction_graph(case_id: int, **filters: Any) -> dict[str, Any]:
    rows = list_bank_transactions(case_id, limit=500, offset=0, **filters)["transactions"]; nodes: dict[str, dict[str, Any]] = {}; edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        account = f"account:{row['account'] or '(空)'}"; party = f"party:{row['counterparty'] or '(空)'}"; nodes.setdefault(account, {"id": account, "label": row["account"] or "(空)", "kind": "account"}); nodes.setdefault(party, {"id": party, "label": row["counterparty"] or "(空)", "kind": "party"})
        source, target = (party, account) if row["direction"] == "inflow" else (account, party); key = (source, target, row["currency"]); edge = edges.setdefault(key, {"from": source, "to": target, "relation_type": "资金链路", "currency": row["currency"], "count": 0, "amount_minor": 0, "transaction_ids": [], "unknown_direction": row["direction"] == "unknown"}); edge["count"] += 1; edge["transaction_ids"].append(row["id"]); edge["amount_minor"] += row["amount_minor"] or 0
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "transaction_count": len(rows)}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cap_total_text(pages: list[str]) -> list[str]:
    """Bound total extracted text so a hostile document cannot exhaust memory."""
    total = 0
    capped: list[str] = []
    for text in pages:
        remaining = MAX_TEXT_CHARS - total
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
        total += len(text)
        capped.append(text)
    if sum(len(x) for x in pages) > MAX_TEXT_CHARS:
        capped[-1] = (capped[-1] if capped else "") + "\n[文本超出提取上限，已截断]"
    return capped


def extract_pdf_pages(path: Path) -> list[str]:
    _check_parse_budget(path)
    info = run_text_command(["pdfinfo", str(path)])
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    count = int(match.group(1)) if match else 1
    if count > MAX_PDF_PAGES:
        raise ValueError(f"PDF 页数超过解析上限（{MAX_PDF_PAGES} 页）")
    pages: list[str] = []
    for page_no in range(1, count + 1):
        text = run_text_command(
            ["pdftotext", "-f", str(page_no), "-l", str(page_no), "-layout", str(path), "-"],
            timeout=60,
        ).strip()
        if len(re.sub(r"\s", "", text)) < 24:
            with tempfile.TemporaryDirectory(prefix="lexvault-ocr-") as tmp:
                base = str(Path(tmp) / "page")
                subprocess.run(
                    ["pdftoppm", "-f", str(page_no), "-l", str(page_no), "-r", "180", "-png", "-singlefile", str(path), base],
                    capture_output=True,
                    timeout=120,
                    check=True,
                )
                text = run_text_command(
                    ["tesseract", f"{base}.png", "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                    timeout=120,
                ).strip()
        pages.append(text)
    return _cap_total_text(pages)


def _table_text(table) -> str:
    rows = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_docx_pages(path: Path) -> list[str]:
    _check_parse_budget(path)
    doc = DocxDocument(path)
    chunks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append("\n".join(current))
            current = []

    # Tables carry legal facts (amounts, parties); drop nothing and keep
    # document order. DOCX has no physical pages, so logical segments are
    # prefixed where a table begins to stay honest about the layout source.
    for block in doc.iter_inner_content():
        if hasattr(block, "text"):  # Paragraph
            text = block.text.strip()
            if not text:
                continue
            current.append(text)
            if sum(len(x) for x in current) >= 1800:
                flush()
        else:  # Table
            flush()
            table = _table_text(block)
            if table:
                chunks.append(f"【表格】\n{table}")
    flush()
    if not chunks:
        chunks.append("")
    return _cap_total_text(chunks)


def extract_image_text(path: Path) -> list[str]:
    _check_parse_budget(path)
    return [run_text_command(["tesseract", str(path), "stdout", "-l", "chi_sim+eng", "--psm", "6"], timeout=120).strip()]


def extract_pages(path: Path, mime_type: str) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_pages(path)
    if suffix == ".docx":
        return extract_docx_pages(path)
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
        return extract_image_text(path)
    if suffix in {".xls", ".xlsx"}:
        _check_parse_budget(path)
        rows = parse_spreadsheet(path.read_bytes(), path.name)
        if not rows:
            return [""]
        lines = [
            f"【工作表：{row.source_sheet or '默认'}｜第{row.source_row_number}行】\n{row.raw_row_json}"
            for row in rows
        ]
        return _cap_total_text(["\n\n".join(lines[i : i + 200]) for i in range(0, len(lines), 200)])
    # The client-provided MIME type is not an authorization to parse an
    # arbitrary active format (HTML/SVG) as a trusted text document.
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        _check_parse_budget(path)
        raw = path.read_text(encoding="utf-8", errors="ignore")
        if "\f" in raw:
            return _cap_total_text([x.strip() for x in raw.split("\f")])
        return _cap_total_text([raw[i : i + 2200] for i in range(0, max(len(raw), 1), 2200)] or [""])
    raise ValueError(f"暂不支持该文件类型：{suffix or mime_type}")


def infer_doc_type(name: str, text: str) -> str:
    haystack = f"{name} {text[:500]}"
    mapping = [
        ("起诉", "起诉意见书"),
        ("笔录", "询问笔录"),
        ("口供", "询问笔录"),
        ("流水", "银行流水"),
        ("审计", "审计报告"),
        ("合同", "合同"),
        ("邮件", "电子数据"),
        ("聊天", "电子数据"),
        ("鉴定", "鉴定意见"),
        ("判决", "裁判文书"),
        ("法规", "法律法规"),
    ]
    return next((label for keyword, label in mapping if keyword in haystack), "其他材料")


def infer_people(text: str) -> str:
    patterns = [
        r"[\u4e00-\u9fff]{1,2}某(?:某)?",
        r"(?:犯罪嫌疑人|被告人|证人|询问人|法定代表人)[:：]?\s*([\u4e00-\u9fff]{2,4})",
    ]
    found: list[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, text[:8000]):
            value = match if isinstance(match, str) else match[0]
            if value not in found and len(value) <= 4:
                found.append(value)
    return "、".join(found[:10])


def infer_dates(text: str) -> str:
    # Match the longest valid month/day alternatives first so values such as
    # “12月” and “31日” are not accepted prematurely as “1月” / “3日”.
    dates = re.findall(
        r"(?:20\d{2})[年./-](?:1[0-2]|0?[1-9])(?:[月./-](?:3[01]|[12]\d|0?[1-9])日?)?",
        text[:12000],
    )
    return " 至 ".join([dates[0], dates[-1]]) if len(dates) > 1 else (dates[0] if dates else "")


def concise(text: str, limit: int = 180) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for marker in ("。", "；", ";", "！", "？"):
        pos = cut.rfind(marker)
        if pos > limit // 2:
            return cut[: pos + 1]
    return cut + "…"


def index_upload(case_id: int, original_name: str, payload: bytes, content_type: str | None, *, import_key: str | None = None) -> dict[str, Any]:
    from .db import get_db_path
    content_hash = hashlib.sha256(payload).hexdigest()
    if import_key:
        with closing(connect()) as conn:
            existing = conn.execute("SELECT id,case_id FROM documents WHERE import_key=?", (import_key,)).fetchone()
        if existing:
            if existing["case_id"] != case_id:
                raise ValueError("Import key belongs to another case")
            return get_document(existing["id"])
    with closing(connect()) as conn:
        same_case = conn.execute(
            "SELECT id FROM documents WHERE case_id=? AND content_hash=? ORDER BY id LIMIT 1",
            (case_id, content_hash),
        ).fetchone()
    if same_case:
        # Identical bytes already indexed for this case: an explicit duplicate,
        # not a silent second copy. Different cases never share this decision.
        document = get_document(same_case["id"])
        document["duplicate"] = True
        return document
    clean_name = safe_filename(original_name)
    case_dir = get_db_path().parent / "uploads" / str(case_id)
    case_dir.mkdir(parents=True, exist_ok=True)
    stored = case_dir / f"{uuid.uuid4().hex[:12]}_{clean_name}"
    stored.write_bytes(payload)
    mime_type = content_type or mimetypes.guess_type(clean_name)[0] or "application/octet-stream"
    duplicate = False
    try:
        page_texts = extract_pages(stored, mime_type)
        combined = "\n".join(page_texts)
        doc_type = infer_doc_type(clean_name, combined)
        people = infer_people(combined)
        date_range = infer_dates(combined)
        summary = concise(combined, 220) or "未识别到有效文本，请人工补充目录信息。"
        ts = now()
        with transaction() as conn:
            inserted = conn.execute(
                """INSERT INTO documents(case_id,name,stored_path,mime_type,pages,status,doc_type,people,date_range,summary,created_at,updated_at,import_key,content_hash)
                   VALUES (?,?,?,?,?,'已索引',?,?,?,?,?,?,?,?)
                   ON CONFLICT(import_key) WHERE import_key IS NOT NULL DO NOTHING""",
                (case_id,clean_name,str(stored),mime_type,len(page_texts),doc_type,people,date_range,summary,ts,ts,import_key,content_hash),
            )
            if inserted.rowcount == 0:
                existing = conn.execute("SELECT id,case_id FROM documents WHERE import_key=?", (import_key,)).fetchone()
                if not existing or existing["case_id"] != case_id:
                    raise ValueError("Import key belongs to another case")
                doc_id, duplicate = existing["id"], True
            else:
                doc_id = inserted.lastrowid
                for page_no, text in enumerate(page_texts, 1):
                    conn.execute("INSERT INTO pages(document_id,page_no,text,summary) VALUES (?,?,?,?)",
                                 (doc_id,page_no,text,concise(text,140)))
                conn.execute("INSERT INTO audit_log(case_id,action,detail,created_at) VALUES (?,'上传并索引文件',?,?)",
                             (case_id,f"{clean_name}，共{len(page_texts)}页",ts))
    except Exception:
        stored.unlink(missing_ok=True)
        raise
    if duplicate:
        stored.unlink(missing_ok=True)
    return get_document(doc_id)


def get_document(document_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if not row:
            return {}
        result = rowdict(row)
        result["page_items"] = [rowdict(x) for x in conn.execute("SELECT id, page_no, summary FROM pages WHERE document_id = ? ORDER BY page_no", (document_id,))]
        return result
    finally:
        conn.close()


def expand_semantics(question: str) -> list[str]:
    expanded: list[str] = []
    for group in SEMANTIC_GROUPS:
        if any(term in question for term in group):
            expanded.extend(group)
    return list(dict.fromkeys(expanded))


def query_terms(question: str) -> list[str]:
    question = re.sub(r"[，。！？；：、,.!?;:\s()（）\[\]【】]", "", question)
    terms = re.findall(r"[A-Za-z0-9_.%-]{2,}|[\u4e00-\u9fff]{2,}", question)
    output: list[str] = []
    for term in terms:
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            chunks = [ch for ch in term if ch not in STOP_CHARS]
            joined = "".join(chunks)
            output.extend(joined[i : i + 2] for i in range(max(0, len(joined) - 1)))
            if len(joined) <= 8:
                output.append(joined)
        else:
            output.append(term)
    output.extend(expand_semantics(question))
    return [x for x in dict.fromkeys(output) if x]


def detect_route(question: str) -> str:
    if any(x in question for x in ["多少", "几份", "数量", "统计", "合计", "总共"]):
        return "目录统计"
    if any(x in question for x in ["矛盾", "对比", "不一致", "分别", "差异"]):
        return "多文档对比"
    if any(x in question for x in ["法律", "法规", "构成要件", "规定", "法条"]):
        return "知识库+卷宗"
    return "事实检索"


def search_pages(case_id: int, question: str, limit: int = 8, *, allow_fallback: bool = True) -> list[dict[str, Any]]:
    terms = query_terms(question)
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT p.id AS page_id, p.page_no, p.text, p.summary,
                   d.id AS document_id, d.name, d.doc_type, d.people, d.date_range
            FROM pages p JOIN documents d ON d.id = p.document_id
            WHERE d.case_id = ?
            """,
            (case_id,),
        ).fetchall()
    finally:
        conn.close()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        item = rowdict(row)
        haystack = f"{item['name']} {item['doc_type']} {item['people']} {item['text']}".lower()
        matches = []
        score = 0.0
        for term in terms:
            count = haystack.count(term.lower())
            if count:
                score += count * max(1.0, len(term) ** 1.25)
                matches.append(term)
        if score:
            item["score"] = round(score, 2)
            item["matches"] = matches[:8]
            item["quote"] = best_quote(item["text"], matches)
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1]["document_id"], x[1]["page_no"]))
    if not scored and allow_fallback:
        fallback = [rowdict(x) for x in rows[:limit]]
        for item in fallback:
            item["score"] = 0
            item["matches"] = []
            item["quote"] = concise(item["text"], 180)
        return fallback
    return [item for _, item in scored[:limit]]


def best_quote(text: str, matches: list[str]) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    positions = [normalized.find(term) for term in matches if normalized.find(term) >= 0]
    pos = min(positions) if positions else 0
    start = max(0, pos - 45)
    end = min(len(normalized), pos + 155)
    prefix = "…" if start else ""
    suffix = "…" if end < len(normalized) else ""
    return prefix + normalized[start:end] + suffix


def statistics_answer(case_id: int, question: str) -> tuple[str, list[dict[str, Any]]]:
    conn = connect()
    try:
        doc_count = conn.execute("SELECT COUNT(*) FROM documents WHERE case_id = ?", (case_id,)).fetchone()[0]
        page_count = conn.execute(
            "SELECT COALESCE(SUM(pages), 0) FROM documents WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
        evidence_count = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id = ?", (case_id,)).fetchone()[0]
        types = conn.execute(
            "SELECT doc_type, COUNT(*) AS n FROM documents WHERE case_id = ? GROUP BY doc_type ORDER BY n DESC", (case_id,)
        ).fetchall()
        people_values = [x[0] for x in conn.execute("SELECT people FROM documents WHERE case_id = ? AND people <> ''", (case_id,))]
    finally:
        conn.close()
    people = []
    for value in people_values:
        for person in re.split(r"[、,，\s]+", value):
            if person and person not in people:
                people.append(person)
    breakdown = "、".join(f"{x['doc_type']} {x['n']}份" for x in types)
    answer = f"当前案件共有 **{doc_count} 份卷宗、{page_count} 页内容、{evidence_count} 条证据事项**。文书构成为：{breakdown or '暂无'}。"
    if any(x in question for x in ["人员", "几个人", "多少人"]):
        answer += f"\n\n目录中识别到 {len(people)} 名相关人员：{'、'.join(people) or '暂无'}。"
    citations = search_pages(case_id, question, 4)
    return answer, citations


def local_llm_available(model_name: str | None = None) -> tuple[bool, str]:
    llm = LLMConfig.from_env()
    desired_model = model_name or llm.model
    try:
        assert_model_endpoint_allowed(llm.base_url)
    except ValueError:
        # Do not even probe an unapproved destination.
        return False, desired_model
    try:
        req = urllib.request.Request(f"{llm.base_url}/models")
        opener = egress_opener()
        with opener.open(req, timeout=2) as response:
            payload = json.load(response)
        ids = [x.get("id", "") for x in payload.get("data", [])]
        return desired_model in ids, desired_model
    except Exception as exc:
        logger.debug("Local model availability probe failed (%s)", type(exc).__name__)
        return False, desired_model


def strip_reasoning(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    # An interrupted reasoning response may contain an opening tag without its
    # closing tag. Never expose that partial scratchpad to the case workspace.
    text = re.sub(r"<think>[\s\S]*$", "", text, flags=re.IGNORECASE).strip()
    planning_markers = ("我们需要回答用户", "需要分析资料", "问题要求", "需要回答用户")
    if any(marker in text[:240] for marker in planning_markers):
        return ""
    return text


def read_json_with_deadline(
    response: Any, timeout: float, *, deadline: float | None = None, max_bytes: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    """Bound urllib response-body reads, including a stalled read after keepalive.

    urllib exposes its socket through HTTPResponse.fp.raw. Resetting its timeout
    for *each* read prevents a final blocking read from gaining another full
    timeout window. In-memory/test streams have no transport to configure.
    This does not independently interrupt OS DNS resolution or header parsing.
    """
    deadline = time.monotonic() + timeout if deadline is None else deadline
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            response.close()
            raise TimeoutError(f"超过 {timeout} 秒总时限")
        transport = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if transport is not None:
            transport.settimeout(remaining)
        try:
            chunk = response.read1(64 * 1024)
        except TimeoutError as exc:
            response.close()
            raise TimeoutError(f"超过 {timeout} 秒总时限") from exc
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > max_bytes:
            response.close()
            raise ValueError("本地模型响应超过大小限制")
    return json.loads(payload)


def call_local_llm(
    question: str,
    route: str,
    contexts: list[dict[str, Any]],
    timeout: int | None = None,
    model_override: str | None = None,
) -> str:
    llm = LLMConfig.from_env()
    timeout = llm.timeout if timeout is None else timeout
    try:
        assert_model_endpoint_allowed(llm.base_url)
    except ValueError as exc:
        logger.error("Model egress rejected: %s", exc)
        raise RuntimeError(str(exc)) from exc
    available, model = local_llm_available(model_override)
    if not available:
        raise RuntimeError(f"本地模型不可用：{model}")
    context_text = "\n\n".join(
        f"[资料{i}｜{item['name']}｜第{item['page_no']}页]\n{item['quote']}"
        for i, item in enumerate(contexts, 1)
    )
    system = (
        "你是运行在律所内网的阅卷助手。只能依据提供的卷宗片段回答，不得虚构事实或法条。"
        "结论与推测必须分开；每个关键事实后用[资料1]格式标注来源。存在矛盾时明确列出。"
        "输出简洁的中文Markdown，并在末尾给出待律师复核事项。"
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"检索路由：{route}\n问题：{question}\n\n卷宗片段：\n{context_text}"},
            ],
            "temperature": llm.temperature,
            "max_tokens": llm.max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{llm.base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = egress_opener()
    deadline = time.monotonic() + timeout
    try:
        with opener.open(req, timeout=timeout) as response:
            payload = read_json_with_deadline(response, timeout, deadline=deadline)
        answer = strip_reasoning(payload["choices"][0]["message"]["content"])
        if not answer:
            raise RuntimeError("本地模型返回了推理草稿或空正文")
        _last_llm_provenance.set(llm_provenance(route, model))
        return answer
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("Local model response failed error_type=%s", type(exc).__name__)
        raise RuntimeError(f"本地模型调用失败（{type(exc).__name__}）") from exc


PROMPT_VERSION = "chat-system-v1"
_last_llm_provenance: ContextVar[dict[str, Any] | None] = ContextVar("lexvault_last_llm_provenance", default=None)


def llm_provenance(route: str, model: str) -> dict[str, Any]:
    """Identity of the exact inference setup behind one answer (E23)."""
    llm = LLMConfig.from_env()
    system = (
        "你是运行在律所内网的阅卷助手。只能依据提供的卷宗片段回答，不得虚构事实或法条。"
        "结论与推测必须分开；每个关键事实后用[资料1]格式标注来源。存在矛盾时明确列出。"
        "输出简洁的中文Markdown，并在末尾给出待律师复核事项。"
    )
    prompt_digest = hashlib.sha256(f"{PROMPT_VERSION}|{route}|{system}".encode()).hexdigest()[:16]
    return {"prompt_version": PROMPT_VERSION, "prompt_sha256_16": prompt_digest,
            "model": model, "temperature": llm.temperature, "max_tokens": llm.max_tokens}


def fallback_answer(question: str, route: str, contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return "当前案件材料中没有检索到足够依据。建议补充关键词或先完善内容级目录，请律师复核。"
    if route == "多文档对比":
        grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for index, item in enumerate(contexts, 1):
            grouped[item["name"]].append((index, item))
        lines = ["已按不同文件分别检索，发现以下可比对内容："]
        for name, items in list(grouped.items())[:5]:
            index, item = items[0]
            lines.append(f"- **{name}**：{item['quote']}（第{item['page_no']}页）[资料{index}]")
        lines.append("\n请律师结合原页核验陈述主体、时间和语境，系统不自动替代质证判断。")
        return "\n".join(lines)
    lines = ["根据当前卷宗，相关材料显示："]
    for index, item in enumerate(contexts[:4], 1):
        lines.append(f"- {item['quote']} [资料{index}]")
    lines.append("\n以上为检索摘要，请律师点击资料卡返回原页复核关键结论。")
    return "\n".join(lines)


def chat(case_id: int, question: str, user_name: str, conversation_id: int | None, use_llm: bool) -> dict[str, Any]:
    # Local import avoids the services -> agents -> services import cycle.
    from .agents import failure_diagnostic, validate_review_answer

    question = question.strip()
    if not question:
        raise ValueError("问题不能为空")
    route = detect_route(question)
    fallback_reason, diagnostic, rejected_validation = None, None, None
    if route == "目录统计":
        answer, contexts = statistics_answer(case_id, question)
        answer += "\n\n以上为当前数据库目录统计，请律师核对材料是否完整。"
        llm_used = False
        provenance = {"mode": "database-statistics"}
        validation = {
            "valid": True, "scope": "database_metadata_counts",
            "checks": {"metadata_query_succeeded": True}, "issues": [],
            "semantic_entailment_checked": False, "amount_calculations_checked": False,
            "lawyer_review_required": True,
        }
        citation_check = "not_applicable"
    else:
        # Keep the prompt context and the clickable citation cards one-to-one.
        contexts = search_pages(case_id, question, 6)
        llm_used = False
        if use_llm:
            try:
                answer = call_local_llm(question, route, contexts)
                candidate_validation = validate_review_answer(answer, contexts)
                if candidate_validation["valid"]:
                    llm_used = True
                    provenance = dict(_last_llm_provenance.get() or llm_provenance(route, LLMConfig.from_env().model))
                else:
                    rejected_validation = candidate_validation
                    diagnostic = {"phase": "chat_validation", "code": "invalid_answer_contract"}
                    logger.warning("Chat validation failed issues=%s", ",".join(candidate_validation["issues"]))
                    raise RuntimeError("model_output_failed_validation")
            except (RuntimeError, OSError, ValueError, TypeError) as exc:
                diagnostic = failure_diagnostic(exc, "chat_llm")
                logger.warning("Chat model failed error_type=%s", type(exc).__name__)
                raise RuntimeError(f"model_unavailable_or_invalid:{type(exc).__name__}") from exc
        else:
            answer = fallback_answer(question, route, contexts)
            provenance = {"mode": "rule-retrieval", "llm_attempted": False}
        if fallback_reason:
            answer += f"\n\n> {fallback_reason}。"
        validation = validate_review_answer(answer, contexts)
        citation_check = "passed" if validation["valid"] else "failed"

    citations = [
        {
            "index": i,
            "document_id": item["document_id"],
            "document_name": item["name"],
            "page": item["page_no"],
            "quote": item["quote"],
            "url": f"/api/documents/{item['document_id']}/file#page={item['page_no']}",
        }
        for i, item in enumerate(contexts[:6], 1)
    ]
    ts = now()
    with transaction() as conn:
        if conversation_id:
            valid = conn.execute(
                "SELECT id, archived_at FROM conversations WHERE id = ? AND case_id = ?", (conversation_id, case_id)
            ).fetchone()
            if not valid:
                conversation_id = None
            elif valid["archived_at"] is not None:
                raise ArchivedConversationError("该会话已归档，只能查看历史记录；如需继续提问，请新建会话")
        if not conversation_id:
            conversation_id = conn.execute(
                "INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (?, ?, ?, ?)",
                (case_id, user_name or "本机律师", concise(question, 30), ts),
            ).lastrowid
        conn.execute(
            "INSERT INTO messages(conversation_id, role, content, citations_json, route, created_at) VALUES (?, 'user', ?, '[]', ?, ?)",
            (conversation_id, question, route, ts),
        )
        conn.execute(
            "INSERT INTO messages(conversation_id, role, content, citations_json, route, provenance_json, created_at) VALUES (?, 'assistant', ?, ?, ?, ?, ?)",
            (conversation_id, answer, json.dumps(citations, ensure_ascii=False), route, json.dumps(provenance, ensure_ascii=False), ts),
        )
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, 'AI阅卷问答', ?, ?)",
            (case_id, f"{user_name or '本机律师'}：{concise(question, 60)}", ts),
        )
    return {
        "conversation_id": conversation_id,
        "answer": answer,
        "route": route,
        "semantic_expansion": expand_semantics(question),
        "citations": citations,
        "llm_used": llm_used,
        "provenance": provenance,
        "validation": validation,
        "citation_check": citation_check,
        "rejected_llm_validation": rejected_validation,
        "fallback_reason": fallback_reason,
        "failure_diagnostic": diagnostic,
    }


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
    candidates = []
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
EXPORT_BLOCK_TYPES = ("case_summary", "catalog_csv", "evidence_table", "qa_log", "static_markdown", "attachments")
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


def _block_has_data(block: dict[str, Any], context: dict[str, Any]) -> bool:
    kind = block["type"]
    if kind == "case_summary" or kind == "static_markdown":
        return True
    if kind == "catalog_csv":
        return bool(context["documents"])
    if kind == "evidence_table":
        return bool(_export_evidence_rows(context["evidence"], block.get("evidence_status")))
    if kind == "qa_log":
        return bool(context["messages"])
    if kind == "attachments":
        return any(
            (Path(doc["stored_path"]) if doc["stored_path"] else None) and Path(doc["stored_path"]).is_file()
            for doc in context["documents"]
        )
    return False


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
    case_id: int, template_id: int | None = None, *, final: bool = False, actor: str = ""
) -> tuple[Path, str]:
    conn = connect()
    try:
        case = rowdict(conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone())
        if not case:
            raise ValueError("案件不存在")
        documents = [rowdict(x) for x in conn.execute("SELECT * FROM documents WHERE case_id = ? ORDER BY id", (case_id,))]
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
        messages = [
            rowdict(x)
            for x in conn.execute(
                """
                SELECT m.*, c.title AS conversation_title, c.user_name FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.case_id = ? ORDER BY m.id
                """,
                (case_id,),
            )
        ]
    finally:
        conn.close()

    if template_id is None:
        blocks, template_info = DEFAULT_EXPORT_BLOCKS, {"id": None, "name": "默认结案包", "builtin": True, "version": None}
    else:
        template = get_export_template(template_id)
        if template is None:
            raise ValueError("结案模板不存在")
        blocks = template["blocks"]
        template_info = {"id": template["id"], "name": template["name"], "builtin": template["builtin"], "version": template["updated_at"]}
    context = {"case": case, "documents": documents, "evidence": evidence, "messages": messages}

    if final:
        missing = [block.get("title") or block["type"] for block in blocks if block.get("required") and not _block_has_data(block, context)]
        if missing:
            raise ValueError("结案包缺少必选内容：" + "、".join(missing))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    export_dir = get_db_path().parent / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{safe_filename(case['title'])}_{stamp}_{uuid.uuid4().hex[:8]}.zip"

    summary = (
        f"# {case['title']}\n\n"
        f"- 案号：{case['case_no']}\n- 类型：{case['case_type']}\n- 当事人：{case['client_name']}\n"
        f"- 状态：{case['status']}\n- 卷宗：{len(documents)}份\n- 证据事项：{len(evidence)}条\n\n"
        f"## 案件说明\n\n{case['description']}\n\n"
        "> 本文件由本地复刻系统生成，所有AI结果均需执业律师复核。\n"
    )
    directory_csv_rows = []
    for doc in documents:
        directory_csv_rows.append([csv_safe_cell(doc[key]) for key in ("name", "doc_type", "pages", "people", "date_range", "summary", "status")])
    directory_csv = io.StringIO()
    writer = csv.writer(directory_csv)
    writer.writerow(["文件名", "文书类型", "页数", "涉及人员", "时间范围", "摘要", "状态"])
    writer.writerows(directory_csv_rows)
    chats = ["# 阅卷问答记录\n"]
    for message in messages:
        chats.append(f"## {message['conversation_title']} · {message['user_name']}\n\n**{message['role']}**\n\n{message['content']}\n")
    if not messages:
        chats.append("\n[待补充：暂无问答记录]")

    transactions = list_bank_transactions(case_id, limit=500, offset=0)["transactions"]
    transaction_csv = io.StringIO()
    transaction_writer = csv.writer(transaction_csv)
    transaction_writer.writerow(["日期", "账号", "方向", "金额（分）", "币种", "对方", "摘要", "来源行", "状态"])
    for row in transactions:
        transaction_writer.writerow([csv_safe_cell(row[key]) for key in ("transaction_time", "account", "direction", "amount_minor", "currency", "counterparty", "memo", "source_row_number", "parse_status")])
    transaction_bytes = ("\ufeff" + transaction_csv.getvalue()).encode("utf-8")
    transaction_sha = hashlib.sha256(transaction_bytes).hexdigest()
    block_renderers = {
        "case_summary": lambda block: summary,
        "catalog_csv": lambda block: "\ufeff" + directory_csv.getvalue(),
        "evidence_table": lambda block: "\ufeff" + _evidence_table_csv(_export_evidence_rows(evidence, block.get("evidence_status"))),
        "qa_log": lambda block: "\n".join(chats),
        "static_markdown": lambda block: block.get("content") or "[待补充：模板未填写内容]",
    }

    manifest = {
        "generated_at": now(),
        "schema_version": SCHEMA_VERSION,
        "renderer_version": EXPORT_RENDERER_VERSION,
        "finalized": bool(final),
        "case": {"id": case["id"], "title": case["title"], "case_no": case["case_no"], "status": case["status"]},
        "template": {**template_info, "blocks": blocks},
        "documents": [
            {"id": doc["id"], "name": doc["name"], "content_hash": doc.get("content_hash"),
             "pages": doc["pages"], "doc_type": doc["doc_type"], "status": doc["status"]}
            for doc in documents
        ],
        "evidence": [
            {"id": item["id"], "title": item["title"], "status": item["status"],
             "approved_by": item.get("approved_by"), "approved_at": item.get("approved_at"),
             "source_document_id": item.get("source_document_id"),
             "source_pages": [item["source_page_start"], item["source_page_end"]]}
            for item in evidence
        ],
        "files": [],
        "artifacts": [{"archive_name": "资金流水.csv", "sha256": transaction_sha, "transaction_count": len(transactions)}],
    }

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("资金流水.csv", transaction_bytes)
        for block in blocks:
            kind = block["type"]
            if kind == "attachments":
                folder = safe_filename(block.get("filename") or block.get("title") or "原始卷宗")
                for doc in documents:
                    stored = Path(doc["stored_path"]) if doc["stored_path"] else None
                    if stored and stored.exists() and stored.is_file():
                        # Export must not package files outside the managed data
                        # directory, even if database metadata was tampered with.
                        stored = contained_path(stored, get_db_path().parent)
                        archive_name = f"{folder}/{safe_filename(doc['name'])}"
                        archive.write(stored, archive_name)
                        manifest["files"].append({
                            "archive_name": archive_name, "document_id": doc["id"],
                            "sha256": _file_sha256(stored), "size": stored.stat().st_size,
                        })
                continue
            archive.writestr(_block_filename(block), block_renderers[kind](block))
        archive.writestr("清单.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with transaction() as conn:
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '导出结案包', ?, ?)",
            (case_id, path.name, now()),
        )
        if final:
            conn.execute(
                "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '模板结案打包', ?, ?)",
                (case_id, f"模板[{template_info['name']}] 操作人[{actor or '未知'}] 包哈希 {_file_sha256(path)[:16]}", now()),
            )
    return path, _file_sha256(path)


def build_export(case_id: int) -> Path:
    """Back-compatible wrapper: default layout, no final gating."""
    return build_export_package(case_id)[0]
