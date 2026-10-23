from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app.db import connect, get_db_path, init_db, transaction
from app.domain_integration import IsolatedDomainIntegration
from app.domain_writes import DOMAIN_RUNTIME
from app.host_integration import IsolatedHostIntegration
from app.runtime_adapter import current_actor_identity
from app.runtime_ports import AccessDeniedError, ModelConfig, RequestConflictError, RunRequest, RunScope
from tests.test_domain_writes import domain  # noqa: F401
from tests.test_forge_readonly import write_corpus
from tests.test_host_writes import isolated  # noqa: F401
from app.security import Principal, _principal

actual = pytest.mark.skipif(not os.getenv("LAW_REVIEW_FORGE_PYTHON"), reason="installed independent Python3.12 required")


def request(**changes):
    return replace(RunRequest(request_id="domain-process", scope=RunScope(1), actor=current_actor_identity(),
        question="合成审批记录的事实和证据是否完整，有哪些疏漏？", runtime=DOMAIN_RUNTIME,
        model=ModelConfig(use_llm=False), use_remote_embeddings=False), **changes)


def counts():
    with closing(connect()) as conn:
        return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in (
            "agent_runs", "agent_steps", "host_write_receipts", "messages", "memories", "gap_detections", "audit_log")}


def process(mode, *, expect_crash=False):
    result = subprocess.run([sys.executable, "-m", "tests.domain_integration_driver", str(get_db_path()), mode],
        cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(), capture_output=True,
        text=True, timeout=45, start_new_session=True)
    if expect_crash:
        assert result.returncode == -signal.SIGKILL, result.stderr
        return None
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_accept_freezes_domain_graph_and_separates_runtime(domain):
    control = IsolatedDomainIntegration()
    first = control.accept(request())
    assert control.accept(request())["duplicate"]
    with closing(connect()) as conn:
        graph = json.loads(conn.execute("SELECT graph_json FROM host_runtime_links").fetchone()[0])
    by_id = {node["id"]: node for node in graph}
    assert by_id["gap_detection"]["dependencies"] == ["retrieve", "facts", "evidence"]
    assert by_id["specialist-join"]["kind"] == "join"
    assert by_id["critic"]["dependencies"] == ["specialist-join"]
    with pytest.raises(AccessDeniedError):
        IsolatedHostIntegration().status(first["request_id"])
    init_db(seed=False, recover_runs=True)
    with closing(connect()) as conn:
        assert conn.execute("SELECT status FROM agent_runs").fetchone()[0] == "running"


def test_view_only_query_does_not_acquire_write_permission(domain, monkeypatch):
    token = "domain-read-token-" + "x" * 32
    config = {token: {"name": "律师", "case_ids": [1], "permissions": ["view", "edit"]}}
    monkeypatch.setenv("LAW_REVIEW_AUTH_MODE", "token")
    monkeypatch.setenv("LAW_REVIEW_API_TOKENS_JSON", json.dumps(config))
    marker = _principal.set(Principal("律师", False, (1,), permissions=frozenset({"view", "edit"})))
    try:
        control = IsolatedDomainIntegration()
        control.accept(request())
        config[token]["permissions"] = ["view"]
        monkeypatch.setenv("LAW_REVIEW_API_TOKENS_JSON", json.dumps(config))
        assert control.status("domain-process")["status"] == "queued"
        with pytest.raises(AccessDeniedError):
            control.cancel("domain-process")
    finally:
        _principal.reset(marker)


@actual
def test_domain_roundtrip_restores_outputs_and_projects_real_citations(domain, monkeypatch):
    control = IsolatedDomainIntegration()
    control.accept(request())
    result = control.drive("domain-process")
    assert result["status"] == "succeeded", json.dumps(result["result"], ensure_ascii=False, indent=2)
    observed = counts()
    assert observed["messages"] == 2 and observed["memories"] == 1 and observed["audit_log"] == 1
    with closing(connect()) as conn:
        row = conn.execute("SELECT final_answer,citations_json FROM agent_runs").fetchone()
        assert row["final_answer"] and json.loads(row["citations_json"])[0]["document_id"] == domain[1]
        assert conn.execute("SELECT kind FROM memories").fetchone()[0] == "runtime_draft"
        assert conn.execute("SELECT status FROM gap_detections LIMIT 1").fetchone()[0] == "待核验"
    # Completion/rebuild can only read stored results. Recalculation is a failure.
    monkeypatch.setattr(control, "_compute", lambda *args: pytest.fail("completed domain node reran"))
    assert control.drive("domain-process")["result"] == result["result"]
    assert control.rebuild_projection("domain-process")["result"] == result["result"]
    assert counts() == observed


@actual
def test_missing_sources_fail_without_success_message_or_draft(domain):
    control = IsolatedDomainIntegration()
    control.accept(request(question="量子香蕉完全无关资料"))
    result = control.drive("domain-process")
    assert result["status"] == "failed", json.dumps(result["result"], ensure_ascii=False, indent=2)
    observed = counts()
    assert observed["messages"] == 1 and observed["memories"] == 0
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_steps WHERE status='failed'").fetchone()[0] == 1


@actual
def test_domain_draft_disabled_and_source_mutation_stops_recovery(domain):
    control = IsolatedDomainIntegration()
    control.accept(request(persist_memory=False))
    assert control.drive("domain-process")["status"] == "succeeded"
    assert counts()["memories"] == 0
    with transaction() as conn:
        conn.execute("UPDATE pages SET text=text||'changed'")
    with pytest.raises(RequestConflictError):
        control.drive("domain-process")


@actual
@pytest.mark.parametrize("boundary", ["accept", "response", "journal", "projection"])
def test_new_process_reuses_proven_domain_results(domain, boundary):
    process(boundary, expect_crash=True)
    recovered = process("normal")
    assert recovered["status"] == "succeeded", recovered["result"]
    observed = counts()
    assert observed["agent_runs"] == 1 and observed["messages"] == 2 and observed["memories"] == 1
    assert observed["audit_log"] == 1
    assert process("normal")["result"] == recovered["result"]
    assert counts() == observed


@actual
@pytest.mark.parametrize("boundary,node", [("node","retrieve"),("inside","retrieve"),("effects","persist-effects")])
def test_dispatched_without_receipt_retains_unknown_and_no_partial_effects(domain, boundary, node):
    process(boundary, expect_crash=True)
    observed = counts()
    recovered = process("normal")
    assert recovered["status"] == "running" and recovered["needs_reconciliation"]
    state = next(n for n in recovered["result"]["nodes"] if n["id"] == node)
    assert state["status"] == "unknown" and state["reservation"]["tool_calls"] == 1
    assert observed["messages"] == 1 and observed["memories"] == 0 and observed["gap_detections"] == 0
    process("normal")
    assert counts() == observed


@actual
def test_domain_approval_and_cancel_have_honest_effects(domain):
    result = process("approval")
    assert result["status"] == "awaiting_approval"
    assert counts()["audit_log"] == 0
    assert process("approve")["status"] == "succeeded"
    observed = counts()
    with pytest.raises(AssertionError):
        process("approve")
    assert counts() == observed


@actual
def test_running_domain_cancel_stops_further_writes(domain):
    result = process("cancel")
    assert result["cancel_requested"] and result["status"] == "cancelled", result
    assert counts()["messages"] == 1 and counts()["memories"] == 0


@actual
def test_domain_statutes_are_independent_sources(domain, tmp_path, monkeypatch):
    corpus = tmp_path / "statutes"
    corpus.mkdir()
    write_corpus(corpus, "仅用于合成领域恢复测试的法条原文。")
    monkeypatch.setenv("LAW_REVIEW_LEGAL_CORPUS_DIR", str(corpus))
    control = IsolatedDomainIntegration()
    control.accept(request(question="刑法第二百六十四条的法条"))
    result = control.drive("domain-process")
    assert result["status"] == "succeeded", result["result"]
    assert "[statute:1]" in result["result"]["final_output"]
    with closing(connect()) as conn:
        output = json.loads(conn.execute("SELECT output_json FROM agent_steps WHERE node_name='finish'").fetchone()[0])["value"]
        assert output["statutory_sources"][0]["document_id"] == "synthetic-264"
        assert output["validation"]["semantic_entailment_checked"] is False


@actual
def test_saved_predecessor_tampering_blocks_recovery(domain):
    control = IsolatedDomainIntegration(require_execution_approval=True)
    control.accept(request())
    assert control.drive("domain-process")["status"] == "awaiting_approval"
    with transaction() as conn:
        conn.execute("UPDATE agent_steps SET output_json='{}' WHERE node_name='retrieve'")
    with pytest.raises(RequestConflictError, match="effect changed"):
        control.drive("domain-process", command="approve")
    assert counts()["audit_log"] == 0


@actual
def test_generated_outputs_do_not_invalidate_another_run(domain):
    control = IsolatedDomainIntegration()
    control.accept(request())
    control.accept(request(request_id="second-domain"))
    assert control.drive("domain-process")["status"] == "succeeded"
    assert control.drive("second-domain")["status"] == "succeeded"
    assert counts()["agent_runs"] == 2 and counts()["messages"] == 4
