from __future__ import annotations

import json
from contextlib import closing

import pytest

from app.answerability import parse_decision
from app.db import connect
from app.runtime_ports import ContractValidationError, ModelConfig, RequestConflictError, UnsupportedCapabilityError
from tests.test_candidate_domain import candidate, candidate_request, actual  # noqa: F401
from tests.test_domain_writes import domain  # noqa: F401
from tests.test_host_writes import isolated  # noqa: F401
from tests.test_local_backends import local_models  # noqa: F401
from tests.test_domain_integration import counts

CONTEXTS = [{"document_id": 1, "page_no": 1, "quote": "审批记录"}]
ANSWER = "审批记录需核验 [资料1]。请律师复核。"


def verdict(decision="supported", **changes):
    return {"decision": decision, "reason": "scripted contract test, not a semantic assessment",
            "findings": [{"text": "审批记录需核验。", "sources": [1]}] if decision == "supported" else [], **changes}


@pytest.mark.parametrize("decision", ["supported", "unrelated", "insufficient"])
def test_decision_contract_and_honest_scope(decision):
    result = parse_decision(json.dumps(verdict(decision)), CONTEXTS)
    assert result["decision"] == decision and not result["semantic_entailment_checked"]


def test_host_supplies_review_reminder_without_relaxing_model_negation():
    result = parse_decision(json.dumps(verdict()), CONTEXTS)
    assert result["host_review_reminder_added"]
    assert result["answer"].endswith("请律师复核原文与上述结论。")
    with pytest.raises(ContractValidationError):
        parse_decision(json.dumps(verdict(findings=[{"text": "无需律师复核。", "sources": [1]}])), CONTEXTS)


def test_sources_are_derived_from_actual_answer_and_range_checked():
    result = parse_decision(json.dumps(verdict()), CONTEXTS)
    assert result["answer_sources"] == [1]
    with pytest.raises(ContractValidationError):
        parse_decision(json.dumps(verdict(findings=[{"text": "另一个来源。", "sources": [2]}])), CONTEXTS)


@pytest.mark.parametrize("changes", [
    {"decision": []}, {"reason": ""}, {"reason": "x" * 501}, {"findings": {}}, {"findings": []},
    {"findings": [{"text": "记录", "sources": [True]}]},
    {"findings": [{"text": "记录", "sources": [0]}]},
    {"findings": [{"text": "记录", "sources": [1,1]}]},
    {"findings": [{"text": "记录", "sources": []}]},
    {"findings": [{"text": "记录[资料1|文件名]", "sources": [1]}]},
    {"findings": [{"text": "", "sources": [1]}]},
    {"decision": "unrelated", "findings": [{"text": "不应回答", "sources": [1]}]},
    {"decision": "insufficient", "findings": [{"text": "不应回答", "sources": [1]}]}, {"extra": True},
])
def test_malformed_or_mixed_decisions_are_system_failures(changes):
    with pytest.raises(ContractValidationError):
        parse_decision(json.dumps(verdict(**changes)), CONTEXTS)


@pytest.mark.parametrize("content", ["not json", '[]', '{"decision":"unrelated","decision":"supported"}'])
def test_ambiguous_json_is_never_a_refusal(content):
    with pytest.raises(ContractValidationError):
        parse_decision(content, CONTEXTS)


@actual
@pytest.mark.parametrize("decision", ["unrelated", "insufficient"])
def test_durable_refusal_has_no_success_effect_and_no_replay(candidate, decision):
    control, calls = candidate
    original = control.models.transport
    def provider(kind, payload, timeout):
        body = original(kind, payload, timeout)
        if kind == "llm":
            body["choices"][0]["message"]["content"] = json.dumps(verdict(decision, reason="问题要求核验卷宗没有的授权，材料不足"), ensure_ascii=False)
        return body
    control.models.transport = provider
    control.accept(candidate_request(control))
    result = control.drive("domain-process")
    assert result["status"] == "failed" and not result["needs_reconciliation"]
    with closing(connect()) as conn:
        value = json.loads(conn.execute("SELECT output_json FROM agent_steps WHERE node_name='finish'").fetchone()[0])["value"]
        assert value["diagnostic"]["code"] == "answerability_rejected"
        assert value["answerability"]["decision"] == decision
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM gap_detections").fetchone()[0] == 0
        assert not conn.execute("SELECT final_answer FROM agent_runs").fetchone()[0]
    observed, before = list(calls), counts()
    assert control.drive("domain-process")["status"] == "failed"
    assert control.rebuild_projection("domain-process")["status"] == "failed"
    assert calls == observed and counts() == before


def test_candidate_cannot_bypass_policy_with_rule_only_request(candidate):
    control, calls = candidate
    with pytest.raises(UnsupportedCapabilityError, match="answerability"):
        control.accept(candidate_request(control, model=ModelConfig(use_llm=False)))
    assert not calls and counts()["agent_runs"] == 0


def test_changed_policy_cannot_reinterpret_saved_run(candidate, monkeypatch):
    control, _ = candidate
    control.accept(candidate_request(control))
    monkeypatch.setattr("app.answerability.SYSTEM_PROMPT", "changed policy")
    with pytest.raises(RequestConflictError, match="policy"):
        control.status("domain-process")
