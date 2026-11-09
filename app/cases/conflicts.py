"""Conflict-of-interest checks (利益冲突检查) on a case's parties.

Before a firm takes a matter it must check that it is not acting, or has not
acted, on the other side. A case lists its parties -- clients, opposing
parties and related parties, each with former names and a unified social
credit code where known -- and every change to that list runs a check against
every other case in the firm, including archived ones and cases behind an
information wall:

* the firm's client here is an opposing party in another matter, or the other
  way round: a **direct** hit;
* a related party here matches a client or opposing party there (or the
  reverse): a **related** hit;
* the same role on both sides (our client was our client before) is not a
  conflict.

Names are compared after normalisation (width, punctuation, the legal-form
suffix of a company name), so the check errs on the side of reporting a
possible match for a person to judge. A check with hits stays ``pending`` until
someone with the manage permission -- not the person who ran it -- approves or
rejects it with a written reason; until then nobody can work on the case
(:func:`enforce_clearance`, called by the access middleware for every write).

Hit details name another case, so each viewer sees only what they may: a hit on
a case behind a wall, or one the viewer is not assigned to, shows that a
conflict exists and which of our parties matched, never the other case.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.storage.db import connect, now, record_security_event, system_access, transaction

router = APIRouter()

ROLE_LABELS = {"client": "委托人", "opposing": "对方当事人", "related": "关联方"}
STATUS_LABELS = {"unchecked": "未检查", "clear": "未发现冲突", "pending": "命中待审批",
                 "approved": "已放行", "rejected": "未获放行"}
#: Legal-form suffixes stripped before comparing company names, longest first.
LEGAL_FORMS = ("股份有限公司", "有限责任公司", "有限公司", "集团有限公司", "集团公司", "集团", "公司")
_PUNCTUATION = re.compile(r"[\s·•．.,，、;；:：()（）\[\]【】{}<>《》'\"“”‘’`\-—_/\\]+")
_CREDIT_CODE = re.compile(r"[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}")
#: Writes allowed while a check is pending or rejected: fixing the parties,
#: re-running or deciding the check, and the case's own housekeeping.
_CLEARANCE_EXEMPT = re.compile(r"^/api/cases/\d+(?:/parties|/conflict-check(?:/decision)?|/archive|/restore|/trash)?$")
BLOCKING = {"pending": "利益冲突检查命中，合伙人放行前不能开展工作",
            "rejected": "利益冲突检查未获放行，不能开展工作"}


def match_key(name: str) -> str:
    """Comparison form of a party name: width, case, punctuation and legal form removed."""
    value = _PUNCTUATION.sub("", unicodedata.normalize("NFKC", name or "").lower())
    for suffix in LEGAL_FORMS:
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            return value[: -len(suffix)]
    return value


class PartyIn(BaseModel):
    role: Literal["client", "opposing", "related"]
    name: str = Field(min_length=2, max_length=120)
    former_names: list[str] = Field(default_factory=list, max_length=10)
    credit_code: str | None = Field(default=None, max_length=18)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if len(match_key(value)) < 2:
            raise ValueError("当事人名称至少需要两个有效字符")
        return value

    @field_validator("former_names")
    @classmethod
    def _former(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value and value.strip()]
        if any(len(match_key(value)) < 2 or len(value) > 120 for value in cleaned):
            raise ValueError("曾用名至少需要两个有效字符，最长 120 字")
        return cleaned

    @field_validator("credit_code")
    @classmethod
    def _credit(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().upper()
        if not _CREDIT_CODE.fullmatch(value):
            raise ValueError("统一社会信用代码应为 18 位")
        return value


class PartiesIn(BaseModel):
    parties: list[PartyIn] = Field(max_length=50)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=10, max_length=500)


def _keys(name: str, former_names: list[str]) -> set[str]:
    return {key for key in (match_key(value) for value in [name, *former_names]) if key}


def _kind(ours: str, theirs: str) -> str | None:
    if {ours, theirs} == {"client", "opposing"}:
        return "direct"
    if "related" in {ours, theirs} and ours != theirs:
        return "related"
    return None


def _digest(parties: list[dict[str, Any]]) -> str:
    canonical = sorted((party["role"], party["name"], tuple(party["former_names"]), party["credit_code"] or "")
                       for party in parties)
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()


def _load_parties(conn: Any, case_id: int | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT p.id, p.case_id, p.role, p.name, p.former_names, p.credit_code, c.title, c.case_no "
           "FROM case_parties p JOIN cases c ON c.id = p.case_id")
    rows = conn.execute(sql + (" WHERE p.case_id = ? ORDER BY p.id" if case_id is not None else " ORDER BY p.id"),
                        (case_id,) if case_id is not None else ()).fetchall()
    return [{"id": row[0], "case_id": row[1], "role": row[2], "name": row[3],
             "former_names": json.loads(row[4] or "[]"), "credit_code": row[5],
             "case_title": row[6], "case_no": row[7]} for row in rows]


def find_conflicts(case_id: int, parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every pairing of one of ``parties`` with a party of another case that is a conflict.

    Runs with system access: the check must see cases behind walls; what a
    viewer is shown is decided later, by :func:`visible_hits`.
    """
    with system_access(), closing(connect()) as conn:
        others = [party for party in _load_parties(conn) if party["case_id"] != case_id]
    hits = []
    for ours in parties:
        our_keys = _keys(ours["name"], ours["former_names"])
        for theirs in others:
            kind = _kind(ours["role"], theirs["role"])
            if kind is None:
                continue
            matched_on = None
            if ours["credit_code"] and ours["credit_code"] == theirs["credit_code"]:
                matched_on = "credit_code"
            elif match_key(ours["name"]) == match_key(theirs["name"]):
                matched_on = "name"
            elif our_keys & _keys(theirs["name"], theirs["former_names"]):
                matched_on = "former_name"
            if matched_on:
                hits.append({
                    "kind": kind, "matched_on": matched_on,
                    "our_party": {"role": ours["role"], "name": ours["name"]},
                    "case_id": theirs["case_id"], "case_title": theirs["case_title"], "case_no": theirs["case_no"],
                    "their_party": {"role": theirs["role"], "name": theirs["name"]},
                })
    hits.sort(key=lambda hit: (hit["kind"] != "direct", hit["case_id"], hit["our_party"]["name"]))
    return hits


def visible_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the current viewer may see of each hit.

    A hit on a walled case, or on a case the viewer is not assigned to, keeps
    only the facts about our own party: a conflict exists, of this kind, on
    this name -- never which matter it is.
    """
    from app.cases.case_walls import hidden_case_ids
    from app.core.security import allowed_case_ids

    hidden = hidden_case_ids()
    allowed = allowed_case_ids()
    shown = []
    for hit in hits:
        if hit["case_id"] in hidden or (allowed is not None and hit["case_id"] not in allowed):
            shown.append({"kind": hit["kind"], "matched_on": hit["matched_on"], "our_party": hit["our_party"],
                          "restricted": True})
        else:
            shown.append({**hit, "restricted": False})
    return shown


def latest_check(conn: Any, case_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, status, hits, parties_digest, requested_by, requested_at, decided_by, decided_at, decision_reason "
        "FROM conflict_checks WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    if row is None:
        return None
    keys = ("id", "status", "hits", "parties_digest", "requested_by", "requested_at",
            "decided_by", "decided_at", "decision_reason")
    check = dict(zip(keys, tuple(row), strict=True))
    check["hits"] = json.loads(check["hits"] or "[]")
    return check


def clearance(case_id: int) -> str:
    """The case's conflict status: the latest check, or ``unchecked``."""
    with closing(connect()) as conn:
        check = latest_check(conn, case_id)
    return check["status"] if check else "unchecked"


def enforce_clearance(case_id: int, method: str, path: str) -> None:
    """Refuse work on a case whose conflict check has not cleared.

    Reading stays possible; fixing the parties, re-running or deciding the
    check, and archiving remain allowed so the situation can be resolved.
    """
    if method not in {"POST", "PUT", "PATCH", "DELETE"} or _CLEARANCE_EXEMPT.match(path):
        return
    status = clearance(case_id)
    if status in BLOCKING:
        raise HTTPException(409, BLOCKING[status])


def _payload(conn: Any, case_id: int) -> dict[str, Any]:
    check = latest_check(conn, case_id)
    parties = [{key: party[key] for key in ("id", "role", "name", "former_names", "credit_code")}
               for party in _load_parties(conn, case_id)]
    if check is not None:
        check = {**check, "hits": visible_hits(check["hits"]), "status_label": STATUS_LABELS[check["status"]]}
        check.pop("parties_digest", None)
    status = check["status"] if check else "unchecked"
    return {"case_id": case_id, "status": status, "status_label": STATUS_LABELS[status],
            "parties": parties, "check": check}


def run_check(case_id: int, actor: str) -> dict[str, Any]:
    """Check the case's current parties against the firm and record the result."""
    with closing(connect()) as conn:
        parties = _load_parties(conn, case_id)
    hits = find_conflicts(case_id, parties)
    status = "pending" if hits else "clear"
    stamp = now()
    direct = sum(hit["kind"] == "direct" for hit in hits)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO conflict_checks(case_id, status, hits, parties_digest, requested_by, requested_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (case_id, status, json.dumps(hits, ensure_ascii=False), _digest(parties), actor, stamp))
        conn.execute("INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '利益冲突检查', ?, ?)",
                     (case_id, f"{actor}：{len(parties)} 个当事人，命中 {len(hits)} 项（直接冲突 {direct} 项）", stamp))
    record_security_event("conflict_check", status, actor=actor, case_id=case_id,
                          detail=f"命中 {len(hits)} 项")
    with closing(connect()) as conn:
        return _payload(conn, case_id)


def replace_parties(conn: Any, case_id: int, parties: list[PartyIn], actor: str) -> None:
    stamp = now()
    conn.execute("DELETE FROM case_parties WHERE case_id = ?", (case_id,))
    for party in parties:
        conn.execute(
            "INSERT INTO case_parties(case_id, role, name, match_key, former_names, credit_code, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, party.role, party.name, match_key(party.name),
             json.dumps(party.former_names, ensure_ascii=False), party.credit_code, actor, stamp))


def _actor() -> str:
    from app.core.security import actor_name

    return actor_name()


@router.get("/api/cases/{case_id}/parties")
def get_parties(case_id: int) -> dict[str, Any]:
    from app.api.routes.common import require_case

    require_case(case_id)
    with closing(connect()) as conn:
        return _payload(conn, case_id)


@router.put("/api/cases/{case_id}/parties")
def put_parties(case_id: int, body: PartiesIn) -> dict[str, Any]:
    """Replace the case's parties and check them against the firm at once."""
    from app.api.routes.common import require_case

    require_case(case_id)
    actor = _actor()
    with transaction() as conn:
        replace_parties(conn, case_id, body.parties, actor)
        conn.execute("INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '更新当事人', ?, ?)",
                     (case_id, f"{actor}：{len(body.parties)} 个当事人", now()))
    return run_check(case_id, actor)


@router.post("/api/cases/{case_id}/conflict-check")
def rerun_check(case_id: int) -> dict[str, Any]:
    """Check again, e.g. after other cases changed their parties."""
    from app.api.routes.common import require_case

    require_case(case_id)
    return run_check(case_id, _actor())


@router.post("/api/cases/{case_id}/conflict-check/decision")
def decide(case_id: int, body: DecisionIn) -> dict[str, Any]:
    """A partner approves or rejects a pending check; never the person who ran it."""
    from app.api.routes.common import require_case
    from app.core.security import current_principal, require_permission

    require_permission("manage")
    require_case(case_id)
    principal = current_principal()
    actor = _actor()
    with closing(connect()) as conn:
        check = latest_check(conn, case_id)
    if check is None or check["status"] != "pending":
        raise HTTPException(409, "当前没有待审批的利益冲突检查")
    if not principal.local and check["requested_by"] == actor:
        raise HTTPException(403, "利益冲突放行需要由检查发起人以外的人决定")
    status = "approved" if body.decision == "approve" else "rejected"
    stamp = now()
    with transaction() as conn:
        updated = conn.execute(
            "UPDATE conflict_checks SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ? "
            "WHERE id = ? AND status = 'pending'", (status, actor, stamp, body.reason, check["id"]))
        if updated.rowcount != 1:
            raise HTTPException(409, "检查状态已变化，请刷新后重试")
        label = "利益冲突放行" if status == "approved" else "利益冲突驳回"
        note = "（本机单人模式，未做双人复核）" if principal.local else ""
        conn.execute("INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                     (case_id, label, f"{actor}：{body.reason}{note}", stamp))
    record_security_event("conflict_decision", status, actor=actor, case_id=case_id, detail=body.reason[:120])
    with closing(connect()) as conn:
        return _payload(conn, case_id)
