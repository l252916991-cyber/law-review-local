"""Bank transaction import and analysis endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.api.routes.common import require_case
from app.core.config import config
from app.core.security import actor_name
from app.review.services import (
    bank_transaction_graph,
    index_upload,
    list_bank_transactions,
    parse_spreadsheet,
    persist_bank_transactions,
    persist_parsed_bank_rows,
    summarize_bank_transactions,
    upload_error_message,
)
from app.storage.db import record_security_event

router = APIRouter()


@router.post("/api/cases/{case_id}/bank-transactions/import", status_code=201)
async def import_bank_transactions(case_id: int, file: UploadFile = File(...)):
    require_case(case_id)
    if not (file.filename or "").lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(400, "流水文件仅支持 CSV、XLSX、XLS")
    payload = await file.read(config.max_upload_size + 1)
    await file.close()
    if len(payload) > config.max_upload_size:
        raise HTTPException(413, "流水文件超过单文件大小限制")
    try:
        result = index_upload(case_id, file.filename or "bank-transactions.csv", payload, file.content_type)
        source_hash = result.get("content_hash", "")
        if (file.filename or "").lower().endswith((".xlsx", ".xls")):
            rows = parse_spreadsheet(payload, file.filename or "")
            persisted = persist_bank_transactions(case_id, result["id"], source_hash, payload) if not rows else persist_parsed_bank_rows(case_id, result["id"], source_hash, rows)
        else:
            persisted = persist_bank_transactions(case_id, result["id"], source_hash, payload)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, upload_error_message(exc)) from exc
    record_security_event("bank_import", "success", actor=actor_name(), case_id=case_id,
                          detail=f"document:{result['id']} rows:{persisted['rows']}", request_path=f"/api/cases/{case_id}/bank-transactions/import")
    return {"document": result, "transactions": persisted}


@router.get("/api/cases/{case_id}/bank-transactions")
def get_bank_transactions(case_id: int, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), account: str | None = None, direction: Literal["inflow", "outflow", "unknown"] | None = None, counterparty: str | None = None, date_from: str | None = None, date_to: str | None = None):
    require_case(case_id)
    return list_bank_transactions(case_id, limit=limit, offset=offset, account=account, direction=direction, counterparty=counterparty, date_from=date_from, date_to=date_to)


@router.get("/api/cases/{case_id}/bank-transactions/summary")
def get_bank_transaction_summary(case_id: int, account: str | None = None, direction: Literal["inflow", "outflow", "unknown"] | None = None, counterparty: str | None = None, date_from: str | None = None, date_to: str | None = None):
    require_case(case_id)
    return summarize_bank_transactions(case_id, account=account, direction=direction, counterparty=counterparty, date_from=date_from, date_to=date_to)


@router.get("/api/cases/{case_id}/bank-transactions/graph")
def get_bank_transaction_graph(case_id: int):
    require_case(case_id)
    return bank_transaction_graph(case_id)

