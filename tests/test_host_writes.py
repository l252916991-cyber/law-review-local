from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app import db
from app.db import connect, db_scope, init_db, now, transaction
from app.host_writes import LocalHostWritePort, create_isolated_database, canonical
from app.runtime_ports import AccessDeniedError, ContractValidationError, RequestConflictError, RunScope, WriteAction, WriteRequest
from app.security import Principal, _principal


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LAW_REVIEW_AUTH_MODE", "local")
    path = tmp_path / "host.sqlite"
    create_isolated_database(path)
    with db_scope(path):
        with transaction() as conn:
            for title in ("合成案件一", "合成案件二"):
                conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", (title, now(), now()))
            conn.execute("INSERT INTO conversations(case_id,title,created_at) VALUES (1,'合成会话',?)", (now(),))
        yield path


def request(port, action, payload, key=None):
    return WriteRequest(action, port.scope, key or action.value, payload, port.current_version())


def run(port):
    return json.loads(port.execute(request(port, WriteAction.RUN_RECORD, {"question": "合成请求"})).detail)["row_id"]


def payloads(run_id):
    return {
        WriteAction.RUN_RECORD: {"question": "合成请求"},
        WriteAction.STEP_RECORD: {"run_id": run_id, "node_name": "step", "output": {"synthetic": True}},
        WriteAction.GAP_DETECTION: {"run_id": run_id, "gap_type": "测试", "severity": "低", "description": "合成缺口",
                                  "suggestion": "复核", "affected_evidence_ids": []},
        WriteAction.MEMORY_DRAFT: {"run_id": run_id, "content": "合成草稿", "importance": 0.5},
        WriteAction.CONVERSATION_MESSAGE: {"conversation_id": 1, "role": "assistant", "content": "合成回复", "citations": []},
        WriteAction.AUDIT_ENTRY: {"detail": "合成操作"},
    }


def test_six_actions_atomic_idempotent_and_query(isolated):
    port = LocalHostWritePort(RunScope(1))
    version = port.current_version()
    run_id = run(port)
    for action, payload in payloads(run_id).items():
        req = request(port, action, payload)
        first = port.execute(req)
        again = port.execute(req)
        queried = port.query_receipt(port.scope, action, req.idempotency_key)
        assert first.receipt_id == again.receipt_id == queried.receipt_id
        assert again.duplicate and again.status == "duplicate"
        assert queried.accepted and queried.status == "applied"
        assert port.current_version() == version
    with closing(connect()) as conn:
        for table in ("agent_runs", "agent_steps", "gap_detections", "memories", "messages", "audit_log", "host_write_receipts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == (6 if table == "host_write_receipts" else 1)
        assert conn.execute("SELECT kind FROM memories").fetchone()[0] == "runtime_draft"
        assert conn.execute("SELECT status FROM gap_detections").fetchone()[0] == "待核验"


def test_same_key_conflicts_and_cross_scope_no_leak(isolated):
    port = LocalHostWritePort(RunScope(1))
    req = request(port, WriteAction.AUDIT_ENTRY, {"detail": "保密合成内容"})
    port.execute(req)
    for changed in (replace(req, payload={"detail": "换内容"}), replace(req, source_version="other")):
        with pytest.raises(RequestConflictError):
            port.execute(changed)
    other = LocalHostWritePort(RunScope(2))
    with pytest.raises(RequestConflictError, match="binding conflict"):
        other.execute(request(other, req.action, req.payload, req.idempotency_key))
    with pytest.raises(AccessDeniedError, match="unavailable"):
        other.query_receipt(other.scope, req.action, req.idempotency_key)
    marker = _principal.set(Principal("另一律师", False, (1,)))
    try:
        new = LocalHostWritePort(RunScope(1))
        with pytest.raises(RequestConflictError):
            new.execute(request(new, req.action, req.payload, req.idempotency_key))
        with pytest.raises(AccessDeniedError):
            new.query_receipt(new.scope, req.action, req.idempotency_key)
    finally:
        _principal.reset(marker)
    assert port.query_receipt(port.scope, WriteAction.AUDIT_ENTRY, "absent") is None


@pytest.mark.parametrize("change", ["case", "evidence", "knowledge", "delete", "archive"])
def test_source_and_lifecycle_rechecked(isolated, change):
    port = LocalHostWritePort(RunScope(1))
    req = request(port, WriteAction.AUDIT_ENTRY, {"detail": "合成"})
    port.execute(req)
    with transaction() as conn:
        if change == "case":
            conn.execute("UPDATE cases SET description='changed' WHERE id=1")
        elif change == "evidence":
            conn.execute("INSERT INTO evidence(case_id,title,created_at) VALUES (1,'changed',?)", (now(),))
        elif change == "knowledge":
            conn.execute("INSERT INTO knowledge(title,category,text) VALUES ('synthetic','test','changed')")
        elif change == "archive":
            conn.execute("UPDATE cases SET lifecycle_status='archived' WHERE id=1")
        else:
            conn.execute("DELETE FROM cases WHERE id=1")
    for operation in (lambda: port.execute(req), lambda: port.query_receipt(port.scope, req.action, req.idempotency_key)):
        with pytest.raises((RequestConflictError, AccessDeniedError)):
            operation()


def test_atomic_rollback_when_receipt_insert_fails(isolated):
    port = LocalHostWritePort(RunScope(1))
    with transaction() as conn:
        conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON host_write_receipts BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        port.execute(request(port, WriteAction.AUDIT_ENTRY, {"detail": "must roll back"}))
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_concurrent_write_one_effect(isolated):
    port = LocalHostWritePort(RunScope(1))
    req = request(port, WriteAction.AUDIT_ENTRY, {"detail": "合成并发"})
    def write():
        with db_scope(isolated):
            return LocalHostWritePort(RunScope(1)).execute(req)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: write(), range(8)))
    assert len({r.receipt_id for r in receipts}) == 1
    assert sum(not r.duplicate for r in receipts) == 1


@pytest.mark.parametrize("bad", [{}, {"detail": "ok", "sql": "bad"}, {"detail": ""}, {"detail": float("nan")}])
def test_payload_rejected_without_effect(isolated, bad):
    port = LocalHostWritePort(RunScope(1))
    with pytest.raises(ContractValidationError):
        port.execute(request(port, WriteAction.AUDIT_ENTRY, bad))
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_scope_run_and_effect_validation(isolated):
    port = LocalHostWritePort(RunScope(1))
    run_id = run(port)
    data = payloads(run_id)
    for action, payload in data.items():
        if "run_id" in payload:
            with pytest.raises(AccessDeniedError):
                port.execute(request(port, action, {**payload, "run_id": 999}))
    req = request(port, WriteAction.MEMORY_DRAFT, data[WriteAction.MEMORY_DRAFT])
    port.execute(req)
    with transaction() as conn:
        conn.execute("UPDATE memories SET content='changed'")
    with pytest.raises(RequestConflictError, match="effect changed"):
        port.query_receipt(port.scope, req.action, req.idempotency_key)


def test_current_token_configuration_rechecked(isolated, monkeypatch):
    token = "a" * 32
    config = {token: {"name": "律师", "case_ids": [1], "permissions": ["view", "edit"]}}
    monkeypatch.setenv("LAW_REVIEW_AUTH_MODE", "token")
    monkeypatch.setenv("LAW_REVIEW_API_TOKENS_JSON", canonical(config))
    marker = _principal.set(Principal("律师", False, (1,), permissions=frozenset({"view", "edit"})))
    try:
        port = LocalHostWritePort(RunScope(1))
        req = request(port, WriteAction.AUDIT_ENTRY, {"detail": "合成"})
        port.execute(req)
        config[token]["case_ids"] = [2]
        monkeypatch.setenv("LAW_REVIEW_API_TOKENS_JSON", canonical(config))
        with pytest.raises(AccessDeniedError):
            port.query_receipt(port.scope, req.action, req.idempotency_key)
    finally:
        _principal.reset(marker)


def test_opt_in_and_schema_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("LAW_REVIEW_AUTH_MODE", "local")
    path = tmp_path / "old.sqlite"
    with db_scope(path):
        with monkeypatch.context() as context:
            context.setattr(db, "SCHEMA_VERSION", 15)
            init_db(seed=False)
        init_db(seed=False)
        init_db(seed=False)
        with closing(connect()) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
            assert conn.execute("SELECT COUNT(*) FROM host_write_receipts").fetchone()[0] == 0
        with pytest.raises(AccessDeniedError, match="synthetic"):
            LocalHostWritePort(RunScope(1))
        with monkeypatch.context() as context:
            context.setattr(db, "SCHEMA_VERSION", 15)
            with pytest.raises(RuntimeError, match="refusing downgrade"):
                init_db(seed=False)
    with pytest.raises(FileExistsError):
        create_isolated_database(path)


@pytest.mark.parametrize("action", list(WriteAction))
def test_each_action_conflict_and_extra_field_rejected(isolated, action):
    port = LocalHostWritePort(RunScope(1))
    run_id = run(port)
    payload = payloads(run_id)[action]
    req = request(port, action, payload)
    first = port.execute(req)
    text_key = next(k for k, v in payload.items() if isinstance(v, str) and k not in {"severity", "role"})
    with pytest.raises(RequestConflictError):
        port.execute(replace(req, payload={**payload, text_key: payload[text_key] + "changed"}))
    with pytest.raises(ContractValidationError):
        port.execute(replace(req, payload={**payload, "unregistered": True}))
    assert port.query_receipt(port.scope, action, req.idempotency_key).receipt_id == first.receipt_id


def test_draft_is_never_recalled_as_validated_fact(isolated):
    from app.rag import recall_memories
    port = LocalHostWritePort(RunScope(1))
    run_id = run(port)
    port.execute(request(port, WriteAction.MEMORY_DRAFT, payloads(run_id)[WriteAction.MEMORY_DRAFT]))
    with transaction() as conn:
        conn.execute("UPDATE agent_runs SET status='completed' WHERE id=?", (run_id,))
    assert recall_memories(1, "合成草稿", prefer_remote_embeddings=False) == []
