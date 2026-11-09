"""Material snapshot a case report was reviewed against.

A report's approval is only meaningful for the exact materials the reviewer saw.
The snapshot records every evidence/document version that existed at submission
time and is hashed so a later export can prove (or refuse) that nothing moved.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from typing import Any

from app.storage.db import connect


def basis_snapshot(conn, case_id: int) -> dict[str, Any]:
    case = conn.execute("SELECT id, version FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not case:
        raise ValueError("案件不存在")
    documents = conn.execute(
        "SELECT id, version, content_hash, pages FROM documents WHERE case_id = ? ORDER BY id", (case_id,)
    ).fetchall()
    evidence = conn.execute(
        "SELECT id, version, status, approved_by, approved_at FROM evidence WHERE case_id = ? ORDER BY id",
        (case_id,),
    ).fetchall()
    return {
        "case": {"id": case["id"], "version": case["version"]},
        "documents": [
            {"id": row["id"], "version": row["version"], "content_hash": row["content_hash"], "pages": row["pages"]}
            for row in documents
        ],
        "evidence": [
            {"id": row["id"], "version": row["version"], "status": row["status"],
             "approved_by": row["approved_by"], "approved_at": row["approved_at"]}
            for row in evidence
        ],
        "counts": {
            "documents": len(documents),
            "pages": sum(row["pages"] for row in documents),
            "evidence": len(evidence),
            "confirmed": sum(1 for row in evidence if row["status"] == "已确认"),
        },
    }


def basis_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def current_basis(case_id: int) -> tuple[dict[str, Any], str]:
    with closing(connect()) as conn:
        snapshot = basis_snapshot(conn, case_id)
    return snapshot, basis_hash(snapshot)
