"""Document intake: parse PDF, DOCX and images into pages, infer metadata and index the upload."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import subprocess
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument

from app.core.files import safe_filename
from app.core.text import concise
from app.documents.bank_transactions import parse_spreadsheet
from app.storage.db import connect, get_db_path, now, rowdict, transaction

logger = logging.getLogger(__name__)


MAX_PARSE_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_TEXT_CHARS = 5_000_000


def _check_parse_budget(path: Path) -> None:
    if path.stat().st_size > MAX_PARSE_BYTES:
        raise ValueError("文件超过解析大小限制")


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
                text = _vision_transcribe(Path(f"{base}.png")) or run_text_command(
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


def _vision_transcribe(path: Path) -> str | None:
    """Best-effort vision transcription; any failure returns ``None``."""
    try:
        from app.documents.vlm_ocr import image_to_text

        return image_to_text(path).strip()
    except Exception as exc:  # disabled, misconfigured, timeout or bad output
        logger.info("Vision transcription skipped error_type=%s", type(exc).__name__)
        return None


def extract_image_text(path: Path) -> list[str]:
    _check_parse_budget(path)
    text = _vision_transcribe(path)
    if text:
        return [text]
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


_DATE_PATTERN = re.compile(r"(20\d{2})\s*[年./-]\s*(\d{1,2})(?:\s*[月./-]\s*(\d{1,2}))?\s*日?")


def _iso_date(match: re.Match[str]) -> str | None:
    year = int(match.group(1))
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    day = match.group(3)
    if day is None or not 1 <= int(day) <= 31:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{int(day):02d}"


def infer_dates(text: str) -> str:
    """Earliest and latest date as YYYY-MM-DD (or YYYY-MM when no day is present).

    Source material mixes 年月日, 2024-01-05 and 2024.01.05, and the timeline,
    gap analysis and mobile sorter all parse one ISO shape, so normalise here.
    """
    values = [
        value
        for value in (_iso_date(match) for match in _DATE_PATTERN.finditer(text[:12000]))
        if value
    ]
    if not values:
        return ""
    earliest, latest = min(values), max(values)
    return earliest if earliest == latest else f"{earliest} 至 {latest}"


def index_upload(case_id: int, original_name: str, payload: bytes, content_type: str | None, *, import_key: str | None = None) -> dict[str, Any]:
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
