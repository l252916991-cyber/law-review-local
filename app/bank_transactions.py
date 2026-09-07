"""Bounded, explicit bank-statement parsing for E31-P3.

CSV remains the canonical interchange format. Spreadsheet support is limited
to value-only reads and feeds the exact same parser/validation contract.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

try:
    import xlrd
except ImportError:  # pragma: no cover
    xlrd = None

PARSER_VERSION = "bank-csv-v1"
MAX_ROWS = 100_000
MAX_CELL_LENGTH = 4_000
MAX_PAYLOAD_BYTES = 50 * 1024 * 1024

ALIASES = {
    "account": ("账号", "卡号", "账户", "本方账号", "账户号码"),
    "direction": ("收支方向", "借贷标志", "借贷方向", "收支"),
    "amount": ("交易金额", "发生额", "金额", "交易数额"),
    "time": ("交易日期", "交易时间", "日期", "入账日期"),
    "counterparty": ("对方户名", "交易对手", "对方账户", "对方账号", "交易对方"),
    "memo": ("摘要", "备注", "附言", "用途"),
    "currency": ("币种", "货币"),
}


def _norm_header(value: Any) -> str:
    return re.sub(r"[\s：:（）()_]", "", str(value or "").strip().lower().lstrip("\ufeff"))


def _header_map(headers: list[Any]) -> dict[str, int]:
    normalized = [_norm_header(item) for item in headers]
    result: dict[str, int] = {}
    for field, aliases in ALIASES.items():
        alias_set = {_norm_header(alias) for alias in aliases}
        match = next((i for i, header in enumerate(normalized) if header in alias_set), None)
        if match is not None:
            result[field] = match
    return result


def _amount_minor(raw: str) -> int | None:
    text = str(raw or "").strip().replace(",", "").replace("，", "")
    text = re.sub(r"(?:人民币|元|CNY|¥|￥)", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _direction(raw: str, amount: int | None) -> str:
    text = str(raw or "").strip().lower()
    if any(x in text for x in ("收入", "贷", "借方", "存入", "入账", "inflow", "credit")):
        return "inflow"
    if any(x in text for x in ("支出", "借", "贷方", "转出", "出账", "outflow", "debit")):
        return "outflow"
    if amount is not None and str(raw).strip().startswith("-"):
        return "outflow"
    return "unknown"


def _date_value(raw: str) -> str | None:
    text = str(raw or "").strip()
    match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", text)
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else None


def _cell_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


@dataclass(frozen=True)
class ParsedTransaction:
    account: str
    direction: str
    amount_minor: int | None
    currency: str
    amount_raw: str
    transaction_time: str | None
    time_raw: str
    counterparty: str
    memo: str
    source_sheet: str
    source_row_number: int
    raw_row_json: str
    parse_status: str
    warnings: list[str]
    row_fingerprint: str


def _parse_rows(headers: Iterable[Any], rows: Iterable[Iterable[Any]], *, sheet: str = "") -> list[ParsedTransaction]:
    headers = [_cell_text(item)[:MAX_CELL_LENGTH] for item in headers]
    mapping = _header_map(headers)
    required = ("account", "amount", "time", "counterparty")
    missing = [field for field in required if field not in mapping]
    parsed: list[ParsedTransaction] = []
    for row_number, values in enumerate(rows, start=2):
        if row_number > MAX_ROWS + 1:
            raise ValueError(f"流水行数超过 {MAX_ROWS} 行上限")
        row = [_cell_text(value)[:MAX_CELL_LENGTH] for value in list(values)[: len(headers)]]
        raw = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}

        def get(field: str) -> str:
            index = mapping.get(field, -1)
            return row[index].strip() if 0 <= index < len(row) else ""

        amount_raw = get("amount")
        amount = _amount_minor(amount_raw)
        time_raw = get("time")
        transaction_time = _date_value(time_raw)
        warnings = [f"missing_{field}" for field in missing]
        for field in ("account", "amount", "time", "counterparty"):
            if field in mapping and not get(field):
                warnings.append(f"missing_{field}_value")
        if amount is None and amount_raw:
            warnings.append("invalid_amount")
        if transaction_time is None and time_raw:
            warnings.append("invalid_date")
        direction = _direction(get("direction"), amount)
        if direction == "unknown":
            warnings.append("missing_direction" if "direction" not in mapping else "ambiguous_direction")
        fingerprint = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        parsed.append(ParsedTransaction(
            account=get("account"), direction=direction,
            amount_minor=abs(amount) if amount is not None else None,
            currency=get("currency") or "CNY", amount_raw=amount_raw,
            transaction_time=transaction_time, time_raw=time_raw,
            counterparty=get("counterparty"), memo=get("memo"), source_sheet=sheet,
            source_row_number=row_number, raw_row_json=json.dumps(raw, ensure_ascii=False),
            parse_status="needs_review" if warnings else "parsed", warnings=warnings,
            row_fingerprint=fingerprint,
        ))
    return parsed


def parse_csv(payload: bytes, *, sheet: str = "") -> list[ParsedTransaction]:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("流水文件超过 50MiB 解析上限")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("gb18030")
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        return []
    return _parse_rows(headers, reader, sheet=sheet)


def parse_matrix(headers: Iterable[Any], rows: Iterable[Iterable[Any]], *, sheet: str) -> list[ParsedTransaction]:
    """Normalize worksheet values through the same bounded parser contract."""
    return _parse_rows(headers, rows, sheet=sheet)


def parse_spreadsheet(payload: bytes, filename: str) -> list[ParsedTransaction]:
    """Read XLSX/XLS values only; formulas and macros are never executed."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("流水文件超过 50MiB 解析上限")
    lower = filename.lower()
    if lower.endswith(".xlsx"):
        if openpyxl is None:
            raise ValueError("缺少 XLSX 解析依赖")
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True, keep_links=False)
        except Exception as exc:
            raise ValueError("XLSX 文件无法解析") from exc
        try:
            output: list[ParsedTransaction] = []
            for worksheet in workbook.worksheets:
                iterator = worksheet.iter_rows(values_only=True)
                headers = list(next(iterator, ()))
                rows = parse_matrix(headers, iterator, sheet=worksheet.title)
                if len(output) + len(rows) > MAX_ROWS:
                    raise ValueError(f"流水行数超过 {MAX_ROWS} 行上限")
                output.extend(rows)
            return output
        finally:
            workbook.close()
    if lower.endswith(".xls"):
        if xlrd is None:
            raise ValueError("缺少 XLS 解析依赖")
        try:
            workbook = xlrd.open_workbook(file_contents=payload, on_demand=True)
        except Exception as exc:
            raise ValueError("XLS 文件无法解析") from exc
        output: list[ParsedTransaction] = []
        for worksheet in workbook.sheets():
            if worksheet.nrows:
                rows = parse_matrix(worksheet.row_values(0), (worksheet.row_values(i) for i in range(1, worksheet.nrows)), sheet=worksheet.name)
                if len(output) + len(rows) > MAX_ROWS:
                    raise ValueError(f"流水行数超过 {MAX_ROWS} 行上限")
                output.extend(rows)
        return output
    raise ValueError("流水文件必须是 CSV、XLSX 或 XLS")
