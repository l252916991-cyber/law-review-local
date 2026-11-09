from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy

import pytest

from app.integration.domain_writes import DomainHostWritePort
from app.integration.host_writes import LocalHostWritePort
from app.integration.runtime_ports import (
    AccessDeniedError,
    ContractValidationError,
    RequestConflictError,
    RunScope,
    WriteAction,
)
from app.storage.db import connect, now, transaction
from tests.test_forge_readonly import write_corpus
from tests.test_host_writes import isolated, request, run  # noqa: F401


@pytest.fixture
def domain(isolated, monkeypatch):
    monkeypatch.delenv("LAW_REVIEW_LEGAL_CORPUS_DIR", raising=False)
    with transaction() as conn:
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
                           "VALUES (1,'合成记录','','text/plain',1,?,?)", (now(), now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,'合成审批记录，固定回报需要核验。')", (doc,))
    return DomainHostWritePort(RunScope(1)), doc


def message(doc):
    return {"conversation_id": 1, "role": "assistant", "content": "合成审批记录 [资料1]。请律师复核。",
            "citations": [{"index": 1, "document_id": doc, "document_name": "合成记录", "page": 1,
                           "quote": "合成审批记录", "url": f"/api/documents/{doc}/file#page=1"}],
            "strict_answer": "合成审批记录 [page:1]「合成审批记录」。请律师复核。", "statutory_sources": []}


def test_domain_steps_and_citations_are_atomic_and_idempotent(domain):
    port, doc = domain
    run_id = run(port)
    version = port.current_version()
    payload = {"run_id": run_id, "node_name": "critic", "role": "审校 Agent", "status": "failed",
               "input": {"retrieval": "pinned"}, "output": {"error": "source_rejected"}}
    step = port.execute(request(port, WriteAction.STEP_RECORD, payload))
    assert port.execute(request(port, WriteAction.STEP_RECORD, payload)).receipt_id == step.receipt_id
    receipt = port.execute(request(port, WriteAction.CONVERSATION_MESSAGE, message(doc)))
    assert port.execute(request(port, WriteAction.CONVERSATION_MESSAGE, message(doc))).receipt_id == receipt.receipt_id
    assert port.current_version() == version
    with closing(connect()) as conn:
        assert conn.execute("SELECT status FROM agent_steps").fetchone()[0] == "failed"
        assert json.loads(conn.execute("SELECT citations_json FROM messages").fetchone()[0])[0]["document_id"] == doc
    # The original synthetic contract has not silently acquired domain capability.
    original = LocalHostWritePort(RunScope(1))
    with pytest.raises(ContractValidationError):
        original.execute(request(original, WriteAction.CONVERSATION_MESSAGE, message(doc), "synthetic"))


@pytest.mark.parametrize("change", ["page", "document", "quote", "name", "url", "strict", "index", "empty"])
def test_invalid_or_forged_citation_never_writes(domain, change):
    port, doc = domain
    payload = message(doc)
    citation = payload["citations"][0]
    if change == "page": citation["page"] = 2
    if change == "document": citation["document_id"] = 999
    if change == "quote": citation["quote"] = "编造原文"
    if change == "name": citation["document_name"] = "伪造文件名"
    if change == "url": citation["url"] = "https://example.invalid/data"
    if change == "strict": payload["strict_answer"] = "[page:1]「编造原文」"
    if change == "index": citation["index"] = True
    if change == "empty": payload.update(citations=[], strict_answer="没有来源")
    with pytest.raises((ValueError, ContractValidationError, AccessDeniedError)):
        port.execute(request(port, WriteAction.CONVERSATION_MESSAGE, payload))
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM host_write_receipts").fetchone()[0] == 0


def test_corpus_identity_and_actual_membership(domain, tmp_path, monkeypatch):
    port, doc = domain
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_corpus(corpus, "合成法条原文。")
    monkeypatch.setenv("LAW_REVIEW_LEGAL_CORPUS_DIR", str(corpus))
    from app.integration.forge_readonly import capture_corpus
    from app.integration.forge_sources import statute_source
    identity, source = capture_corpus()
    hit = source.lookup("刑法", 264, version_date="2020-01-01")[0]
    payload = message(doc)
    payload.update(citations=[], content="仅列法条，请律师复核。",
        strict_answer="[statute:1]「合成法条原文」", statutory_sources=[statute_source(hit, identity, 1)])
    port.execute(request(port, WriteAction.CONVERSATION_MESSAGE, payload))
    forged = deepcopy(payload)
    forged["statutory_sources"][0]["document_id"] = "forged"
    with pytest.raises(ContractValidationError, match="member"):
        port.execute(request(port, WriteAction.CONVERSATION_MESSAGE, forged, "forged"))
    old_version = port.current_version()
    write_corpus(corpus, "变化后的法条。")
    assert port.current_version() != old_version
    with pytest.raises(RequestConflictError, match="source version"):
        port.query_receipt(port.scope, WriteAction.CONVERSATION_MESSAGE, WriteAction.CONVERSATION_MESSAGE.value)


def test_composite_receipt_rejects_modified_child(domain):
    port, _ = domain
    run_id = run(port)
    child = port.execute(request(port, WriteAction.MEMORY_DRAFT,
        {"run_id": run_id, "content": "合成草稿", "importance": 0.5}, "parent:effect:draft"))
    step = {"run_id": run_id, "node_name": "persist-effects", "role": "宿主效果", "status": "completed", "input": {},
            "output": {"value": {}, "_effect_receipts": [child.receipt_id]}}
    port.execute(request(port, WriteAction.STEP_RECORD, step, "parent"))
    with transaction() as conn:
        conn.execute("UPDATE memories SET content='changed'")
    with pytest.raises(RequestConflictError, match="effect changed"):
        port.query_receipt(port.scope, WriteAction.STEP_RECORD, "parent")


@pytest.mark.parametrize("pages,length,extra,allowed", [(500,4000,0,True), (501,1,0,False),
    (500,4000,1,False), (1,20001,0,False)])
def test_confirmed_capacity_is_an_exact_reject_boundary(domain, pages, length, extra, allowed):
    port, doc = domain
    with transaction() as conn:
        conn.execute("DELETE FROM pages WHERE document_id=?", (doc,))
        conn.executemany("INSERT INTO pages(document_id,page_no,text) VALUES (?,?,?)",
                         [(doc, i+1, "字" * (length + (extra if i == 0 else 0))) for i in range(pages)])
    if allowed:
        assert port.current_version()
    else:
        with pytest.raises(ContractValidationError, match="capacity"):
            port.current_version()
