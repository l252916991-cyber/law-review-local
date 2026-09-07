"""Bounded, explicit bank-statement CSV parsing for E31-P3."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

PARSER_VERSION = "bank-csv-v1"
MAX_ROWS = 100_000
MAX_CELL_LENGTH = 4_000

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


def _header_map(headers: list[str]) -> dict[str, int]:
    normalized = [_norm_header(item) for item in headers]
    result = {}
    for field, aliases in ALIASES.items():
        matches = [i for i, header in enumerate(normalized) if header in {_norm_header(a) for a in aliases}]
        if matches:
            result[field] = matches[0]
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
    # A signed amount can provide direction only when the source has no
    # explicit direction column; retain positive amount separately.
    if amount is not None and str(raw).strip().startswith("-"):
        return "outflow"
    return "unknown"


def _date_value(raw: str) -> str | None:
    text = str(raw or "").strip()
    match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})", text)
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else None


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


def parse_csv(payload: bytes, *, sheet: str = "") -> list[ParsedTransaction]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("gb18030")
    if len(text.encode("utf-8")) > 50 * 1024 * 1024:
        raise ValueError("流水文件超过 50MiB 解析上限")
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        return []
    mapping = _header_map(headers)
    required = ("account", "amount", "time", "counterparty")
    missing = [field for field in required if field not in mapping]
    parsed: list[ParsedTransaction] = []
    for row_number, row in enumerate(reader, start=2):
        if row_number > MAX_ROWS + 1:
            raise ValueError(f"流水行数超过 {MAX_ROWS} 行上限")
        if len(row) > len(headers):
            row = row[: len(headers)]
        raw = {headers[i]: str(row[i] if i < len(row) else "")[:MAX_CELL_LENGTH] for i in range(len(headers))}
        get = lambda field: str(row[mapping[field]] if field in mapping and mapping[field] < len(row) else "").strip()
        amount_raw = get("amount")
        amount = _amount_minor(amount_raw)
        transaction_time = _date_value(get("time"))
        warnings = list(f"missing_{field}" for field in missing)
        if amount is None and amount_raw:
            warnings.append("invalid_amount")
        if transaction_time is None and get("time"):
            warnings.append("invalid_date")
        direction = _direction(get("direction"), amount)
        if direction == "unknown":
            warnings.append("missing_direction" if "direction" not in mapping else "ambiguous_direction")
        fingerprint = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        parsed.append(ParsedTransaction(
            account=get("account"), direction=direction, amount_minor=abs(amount) if amount is not None else None,
            currency=get("currency") or "CNY", amount_raw=amount_raw, transaction_time=transaction_time,
            time_raw=get("time"), counterparty=get("counterparty"), memo=get("memo"), source_sheet=sheet,
            source_row_number=row_number, raw_row_json=json.dumps(raw, ensure_ascii=False),
            parse_status="needs_review" if warnings else "parsed", warnings=warnings, row_fingerprint=fingerprint,
        ))
    return parsed
