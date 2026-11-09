from __future__ import annotations

import json
from contextlib import closing

import pytest

from app.integration.runtime_ports import (
    ContractValidationError,
    ModelConfig,
    RequestConflictError,
    UnsupportedCapabilityError,
)
from app.llm.local_backends import LocalModels
from app.review.answerability import (
    ANSWERABILITY_GRAMMAR,
    GRAMMAR_VERSION,
    POLICY_VERSION,
    SYSTEM_PROMPT,
    ContentFreeFinding,
    answer_body,
    answerability_payload,
    content_free_finding,
    parse_decision,
    policy_identity,
)
from app.storage.db import connect
from tests.test_candidate_domain import actual, candidate, candidate_request  # noqa: F401
from tests.test_domain_integration import counts
from tests.test_domain_writes import domain  # noqa: F401
from tests.test_host_writes import isolated  # noqa: F401
from tests.test_local_backends import local_models  # noqa: F401

CONTEXTS = [{"document_id": 1, "page_no": 1, "quote": "审批记录"}]
ANSWER = "审批记录需核验 [资料1]。请律师复核。"
QUESTION = "问题"
CANDIDATE_QUESTION = "合成审批记录的事实和证据是否完整，有哪些疏漏？"


def verdict(decision="supported", **changes):
    return {"decision": decision, "reason": "scripted contract test, not a semantic assessment",
            "findings": [{"text": "审批记录需核验。", "sources": [1]}] if decision == "supported" else [], **changes}


@pytest.mark.parametrize("decision", ["supported", "unrelated", "insufficient"])
def test_decision_contract_and_honest_scope(decision):
    result = parse_decision(json.dumps(verdict(decision)), CONTEXTS, question=QUESTION)
    assert result["decision"] == decision and not result["semantic_entailment_checked"]


def test_specificity_contract_names_every_required_question_dimension():
    """v11: the prompt must demand the recorded item the question asks for.

    The host cannot judge from a short answer whether, say, a company category
    or a holding conclusion was omitted, so the only enforceable place for this
    requirement is the frozen protocol text itself. These assertions fail if a
    later edit silently drops one of the dimensions.
    """
    for dimension in ("时间", "地点", "金额", "数量", "公司或主体类别", "认定结论"):
        assert dimension in SYSTEM_PROMPT
    for conclusion in ("无效", "属于陷阱条款", "支持", "驳回"):
        assert conclusion in SYSTEM_PROMPT
    for vague_substitute in ("很远", "关联公司", "转嫁风险"):
        assert vague_substitute in SYSTEM_PROMPT
    assert "只输出 JSON" in SYSTEM_PROMPT
    identity = policy_identity()
    assert identity["version"] == POLICY_VERSION == "candidate-answerability-v11-detail"
    assert identity["grammar_version"] == GRAMMAR_VERSION


def test_grammar_binding_and_payload_are_explicit(local_models):
    fixture, _ = local_models
    names = {kind: kind for kind in ("llm", "embedding", "reranker")}
    plain = LocalModels(endpoint=fixture.endpoint, identities=fixture.identities, models=names)
    grammar = LocalModels(endpoint=fixture.endpoint, identities=fixture.identities, models=names,
                          answerability_grammar=True)

    assert "answerability_grammar" not in plain.binding
    assert grammar.binding["answerability_grammar"] == GRAMMAR_VERSION
    assert grammar.fingerprint != plain.fingerprint
    assert "guided_grammar" not in answerability_payload(plain, "问题", "route", CONTEXTS)
    payload = answerability_payload(grammar, "问题", "route", CONTEXTS)
    assert payload["guided_grammar"] == ANSWERABILITY_GRAMMAR
    assert "问题：问题" in payload["messages"][1]["content"]
    assert "route" not in payload["messages"][1]["content"]
    assert policy_identity()["grammar_version"] == GRAMMAR_VERSION


def test_host_supplies_review_reminder_without_relaxing_model_negation():
    result = parse_decision(json.dumps(verdict()), CONTEXTS, question=QUESTION)
    assert result["host_review_reminder_added"]
    assert result["answer"].endswith("请律师复核原文与上述结论。")
    with pytest.raises(ContractValidationError):
        parse_decision(json.dumps(verdict(findings=[{"text": "无需律师复核。", "sources": [1]}])), CONTEXTS, question=QUESTION)


def test_explicit_no_record_is_a_supported_limited_scope_answer():
    decision = verdict(findings=[{"text": "本次材料未见交付回执。", "sources": [1]}])
    result = parse_decision(json.dumps(decision), CONTEXTS, question=QUESTION)
    assert result["decision"] == "supported"
    assert "未见" in result["answer"] and result["answer_sources"] == [1]


def test_sources_are_derived_from_actual_answer_and_range_checked():
    result = parse_decision(json.dumps(verdict()), CONTEXTS, question=QUESTION)
    assert result["answer_sources"] == [1]
    with pytest.raises(ContractValidationError):
        parse_decision(json.dumps(verdict(findings=[{"text": "另一个来源。", "sources": [2]}])), CONTEXTS, question=QUESTION)


def test_record_claim_without_content_fails_closed():
    """The S07 training-facts defect must not render as a supported answer."""
    defect = verdict(findings=[{"text": "培训费缴纳与培训地点变更均明确记载", "sources": [1]}])
    with pytest.raises(ContentFreeFinding) as error:
        parse_decision(json.dumps(defect), CONTEXTS, question="培训费缴纳与培训地点变更有什么明确记载？")
    assert error.value.index == 1 and error.value.text == "培训费缴纳与培训地点变更均明确记载"
    assert content_free_finding("合成审批记录的事实和证据均明确记载", CANDIDATE_QUESTION)
    with pytest.raises(ContentFreeFinding):
        parse_decision(json.dumps(verdict(findings=[{"text": "合成审批记录的事实和证据均明确记载",
                                                     "sources": [1]}])),
                       CONTEXTS, question=CANDIDATE_QUESTION)


@pytest.mark.parametrize("text,question", [
    ("仲裁委员会驳回某技术公司的全部仲裁请求。", "仲裁委员会如何处理该公司的仲裁请求？"),
    ("某技术公司请求崔某支付竞业限制违约金并继续履行竞业限制义务。", "仲裁委员会如何处理该公司的仲裁请求？"),
    ("培训费3000元，签约当天即交纳全部培训费。", "培训费缴纳与培训地点变更有什么明确记载？"),
    ("本次材料未见交付回执。", "有无交付回执？"),
    ("材料中未见记载竞业限制补偿的支付凭证。", "有无竞业限制补偿的支付凭证？"),
    ("培训费已交纳。", "培训费是否已交纳？"),
    ("重庆某公司单方更换培训地点至两江新区金开大道。", "培训地点有无变更？"),
])
def test_concrete_facts_roles_and_absence_answers_are_not_content_free(text, question):
    assert not content_free_finding(text, question)


def test_answer_body_excludes_host_citations_and_review_reminder():
    result = parse_decision(json.dumps(verdict()), CONTEXTS, question=QUESTION)
    assert answer_body(result) == "审批记录需核验。"
    assert "[资料" not in answer_body(result) and "请律师复核" not in answer_body(result)
    refusal = parse_decision(json.dumps(verdict("insufficient")), CONTEXTS, question=QUESTION)
    assert answer_body(refusal) == "" and answer_body({}) == ""


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
        parse_decision(json.dumps(verdict(**changes)), CONTEXTS, question=QUESTION)


@pytest.mark.parametrize("content", ["not json", '[]', '{"decision":"unrelated","decision":"supported"}'])
def test_ambiguous_json_is_never_a_refusal(content):
    with pytest.raises(ContractValidationError):
        parse_decision(content, CONTEXTS, question=QUESTION)


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


@actual
def test_content_free_answer_fails_closed_without_success_effects(candidate):
    control, calls = candidate
    original = control.models.transport

    def provider(kind, payload, timeout):
        body = original(kind, payload, timeout)
        if kind == "llm":
            body["choices"][0]["message"]["content"] = json.dumps(
                {"decision": "supported", "reason": "材料有相关记录",
                 "findings": [{"text": "合成审批记录的事实和证据均明确记载", "sources": [1]}]}, ensure_ascii=False)
        return body

    control.models.transport = provider
    control.accept(candidate_request(control))
    result = control.drive("domain-process")
    assert result["status"] == "failed" and not result["needs_reconciliation"]
    with closing(connect()) as conn:
        value = json.loads(conn.execute("SELECT output_json FROM agent_steps WHERE node_name='finish' "
            "AND status='failed'").fetchone()[0])["value"]
        assert value["diagnostic"] == {"code": "content_free_finding", "finding_index": 1}
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert not conn.execute("SELECT final_answer FROM agent_runs").fetchone()[0]
    observed, before = list(calls), counts()
    assert control.drive("domain-process")["status"] == "failed"
    assert calls == observed and counts() == before


def test_candidate_cannot_bypass_policy_with_rule_only_request(candidate):
    control, calls = candidate
    with pytest.raises(UnsupportedCapabilityError, match="answerability"):
        control.accept(candidate_request(control, model=ModelConfig(use_llm=False)))
    assert not calls and counts()["agent_runs"] == 0


def test_changed_policy_cannot_reinterpret_saved_run(candidate, monkeypatch):
    control, _ = candidate
    control.accept(candidate_request(control))
    monkeypatch.setattr("app.review.answerability.SYSTEM_PROMPT", "changed policy")
    with pytest.raises(RequestConflictError, match="policy"):
        control.status("domain-process")
