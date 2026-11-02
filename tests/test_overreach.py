"""Answers that decide a legal question for the lawyer are reported by the gate.

Sentences are from the 2026-11-02 model gate answers (synthetic case), shortened.
The rules were written after reading those answers, so these are in-sample.
"""
import pytest

from app.answer_eval import overreach_findings


@pytest.mark.parametrize("sentence,rule", [
    ("以邮件中确认的“固定回报”内容，作为认定其主观上明知并认可保本保息性质的关键证据，从而推翻其“不知情”的辩解。",
     "mental_state"),
    ("在法律事实认定中，承诺固定回报且到期还本付息，通常被视为保本保息的实质表现。", "legal_characterisation"),
    ("这证明其并非完全不知情，而是对包含固定回报内容的宣传材料进行了审批和推动。", "mental_state"),
    ("宣传页面公开且无合格投资人核验，张某对此应有认知。", "mental_state"),
    ("这种固定回报承诺通常不符合合法私募融资特征，进一步削弱张某“合法融资”主张的可信度。", "defence_refuted"),
    ("该行为构成非法吸收公众存款罪。", "legal_characterisation"),
    ("这佐证了其主观上对业务性质及客户来源存在误解。", "mental_state"),
    ("其说法与其推动公开宣传的行为存在矛盾，可作为其主观明知或应知客户来源非特定性的佐证。", "mental_state"),
    ("张某的辩解不能成立。", "defence_refuted"),
    ("据此认定张某为主犯。", "own_finding"),
])
def test_findings_the_answer_makes_on_its_own_are_reported(sentence, rule):
    findings = overreach_findings(sentence)
    assert [finding["rule"] for finding in findings] == [rule]


@pytest.mark.parametrize("sentence", [
    "请律师确认张某“没看到表述”但“执行固定版本”的行为是否足以认定其主观明知。",   # a question
    "需律师判断张某的否认是否构成对关键事实的回避或误解。",
    "侦查机关认定，2024年1月至2025年3月期间，公司向社会公众募集资金[资料1]。",       # the source's finding
    "李某关于“张某主导决策、明知无牌照仍要求先行开展”的陈述与张某的说法冲突。",       # reported speech
    "本轮卷宗片段不足以回答最终确认的未兑付金额。",
    "不能直接认定资料1中的120万包含了资料2中的80万。",
    "邮件仅证明张某后续批准了该方案，并未改变方案最初由市场部提出的事实。",
])
def test_questions_attributions_and_limits_are_not_findings(sentence):
    assert overreach_findings(sentence) == []


def test_each_sentence_is_reported_once_with_its_text():
    answer = "这证明其知情。在法律上通常被视为保本保息的实质表现。请律师复核原文与上述结论。"
    findings = overreach_findings(answer)
    assert [finding["rule"] for finding in findings] == ["mental_state", "legal_characterisation"]
    assert findings[0]["text"] == "这证明其知情。"


def test_the_gate_counts_only_model_answers_and_never_changes_pass_or_fail():
    from app.answer_eval import load_dataset
    from app.eval_gate import DEFAULT_DATASET, evaluate

    dataset = load_dataset(DEFAULT_DATASET)
    item = dataset["items"][0]
    answers = {row["id"]: {"answer": "", "citations": [], "answer_status": "model_error", "llm_used": False,
                           "latency_ms": 1.0} for row in dataset["items"]}
    claim = "这证明其明知。"
    answers[item["id"]] = {**answers[item["id"]], "answer": claim}
    assert evaluate(dataset, answers)["summary"]["overreach_answers"] == 0   # rule output: not the model's words
    answers[item["id"]]["llm_used"] = True
    report = evaluate(dataset, answers)
    assert report["summary"]["overreach_answers"] == 1
    assert report["items"][item["id"]]["overreach"][0]["rule"] == "mental_state"
    assert report["items"][item["id"]]["passed"] is False  # decided by the score, as before
