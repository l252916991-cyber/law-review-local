"""Persisted bank transactions: storage of parsed rows, filtered listing, summaries and the
counterparty graph."""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from app.documents.bank_transactions import PARSER_VERSION, parse_csv
from app.storage.db import connect, now, rowdict, transaction


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


def _bank_transaction_clause(case_id: int, *, account: str | None = None, direction: str | None = None, counterparty: str | None = None, date_from: str | None = None, date_to: str | None = None) -> tuple[str, list[Any]]:
    clauses = ["case_id=?"]; params: list[Any] = [case_id]
    for field, value in (("account", account), ("counterparty", counterparty)):
        if value: clauses.append(f"{field} LIKE ?"); params.append(f"%{value}%")
    if direction: clauses.append("direction=?"); params.append(direction)
    if date_from: clauses.append("transaction_time>=?"); params.append(date_from)
    if date_to: clauses.append("transaction_time<=?"); params.append(date_to)
    return " AND ".join(clauses), params


def iter_bank_transactions(case_id: int, **filters: Any) -> Iterator[dict[str, Any]]:
    """Stream every matching row for aggregation and export.

    The interactive list endpoint stays paginated and capped at 500 rows, but
    summary, graph and the export package must not report one page as the whole
    case.
    """
    where, params = _bank_transaction_clause(case_id, **filters)
    conn = connect()
    try:
        for row in conn.execute(
            f"SELECT * FROM bank_transactions WHERE {where} ORDER BY transaction_time, id", params
        ):
            yield rowdict(row)
    finally:
        conn.close()


def list_bank_transactions(case_id: int, *, limit: int = 100, offset: int = 0, **filters: Any) -> dict[str, Any]:
    limit = max(1, min(limit, 500)); offset = max(0, offset)
    where, params = _bank_transaction_clause(case_id, **filters)
    conn = connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM bank_transactions WHERE {where}", params).fetchone()[0]
        rows = [rowdict(row) for row in conn.execute(f"SELECT * FROM bank_transactions WHERE {where} ORDER BY transaction_time, id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()]
    finally: conn.close()
    return {"transactions": rows, "pagination": {"total": total, "limit": limit, "offset": offset}}


def summarize_bank_transactions(case_id: int, **filters: Any) -> dict[str, Any]:
    count = inflow_count = outflow_count = 0
    inflow_minor = outflow_minor = 0
    by_account: dict[str, int] = {}
    by_counterparty: dict[str, int] = {}
    by_period: dict[str, int] = {}
    currency = "CNY"
    for row in iter_bank_transactions(case_id, **filters):
        amount = row["amount_minor"]
        if count == 0:
            currency = row["currency"] or "CNY"
        count += 1
        if amount is None:
            continue
        if row["direction"] == "inflow":
            inflow_count += 1; inflow_minor += amount
        elif row["direction"] == "outflow":
            outflow_count += 1; outflow_minor += amount
        by_account[row["account"] or "(空)"] = by_account.get(row["account"] or "(空)", 0) + amount
        by_counterparty[row["counterparty"] or "(空)"] = by_counterparty.get(row["counterparty"] or "(空)", 0) + amount
        if row["transaction_time"]:
            period = row["transaction_time"][:7]
            by_period[period] = by_period.get(period, 0) + amount
    return {"transaction_count": count, "inflow_count": inflow_count, "outflow_count": outflow_count, "inflow_minor": inflow_minor, "outflow_minor": outflow_minor, "net_minor": inflow_minor - outflow_minor, "currency": currency, "by_account": by_account, "by_counterparty": by_counterparty, "by_period": by_period, "filters": filters}


def bank_transaction_graph(case_id: int, **filters: Any) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}; edges: dict[tuple[str, str, str], dict[str, Any]] = {}; count = 0
    for row in iter_bank_transactions(case_id, **filters):
        count += 1
        account = f"account:{row['account'] or '(空)'}"; party = f"party:{row['counterparty'] or '(空)'}"; nodes.setdefault(account, {"id": account, "label": row["account"] or "(空)", "kind": "account"}); nodes.setdefault(party, {"id": party, "label": row["counterparty"] or "(空)", "kind": "party"})
        source, target = (party, account) if row["direction"] == "inflow" else (account, party); key = (source, target, row["currency"]); edge = edges.setdefault(key, {"from": source, "to": target, "relation_type": "资金链路", "currency": row["currency"], "count": 0, "amount_minor": 0, "transaction_ids": [], "unknown_direction": row["direction"] == "unknown"}); edge["count"] += 1; edge["transaction_ids"].append(row["id"]); edge["amount_minor"] += row["amount_minor"] or 0
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "transaction_count": count}
