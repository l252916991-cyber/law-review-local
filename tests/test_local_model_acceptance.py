"""Frozen-value checks reject wrong answers and unsupported or unavailable citations."""
from copy import deepcopy

from scripts.local_model_acceptance import assess_answer, assess_backends, known_value_in_text, normalize, p95


def fixture():
    question = {"kind": "fact", "required_any": [["128000"]],
                "support": [{"document": "contract", "page": 1, "tokens": ["128000"]}]}
    sources = {"contract": {"pages": ["借款本金128000元。", "到期日2026年9月30日。"]}}
    documents = {"contract": {"id": 42}}
    response = {"answer": "本金128,000元[资料1]。", "llm_used": True, "citation_check": "passed",
                "citations": [{"index": 1, "document_id": 42, "page": 1, "quote": "借款本金128000元。"}]}
    return question, sources, documents, response


def test_frozen_value_needs_support_from_referenced_page():
    item, sources, documents, response = fixture()
    assert assess_answer(item, response, documents, sources)["passed"]
    for mutation in ("wrong_amount", "wrong_page", "nonexistent_document", "unreferenced", "quote_unsupported"):
        changed = deepcopy(response)
        if mutation == "wrong_amount":
            changed["answer"] = "本金130000元[资料1]。"
        elif mutation == "wrong_page":
            changed["citations"][0]["page"] = 2
        elif mutation == "nonexistent_document":
            changed["citations"][0]["document_id"] = 99
        elif mutation == "unreferenced":
            changed["answer"] = "本金128000元[资料2]。"
        else:
            changed["citations"][0]["quote"] = "未提供本金。"
        assert not assess_answer(item, changed, documents, sources)["passed"], mutation


def test_safe_refusal_is_explicit_and_has_no_factual_citations():
    item, sources, documents, _ = fixture()
    item["kind"] = "missing"
    response = {"answer_status": "insufficient_evidence", "answer": "资料不足，请补充原件。",
                "citations": [], "llm_used": False, "validation": {"valid": True}}
    assert assess_answer(item, response, documents, sources)["safe_nonfactual_refusal"]
    response["answer"] = "借款本金128000元。"
    assert not assess_answer(item, response, documents, sources)["passed"]


def test_backend_checks_require_exact_real_models_without_hashed_fallback():
    models = {"llm": "chat-model", "embedding": "embedding-model", "reranker": "reranker-model"}
    response = {"llm_used": True, "provenance": {"model": "chat-model"}, "retrieval_metrics": {
        "embedding": {"backend": "omlx", "model": "embedding-model@revision|v1", "degraded": False},
        "reranker": {"enabled": True, "model": "reranker-model"}}}
    assert assess_backends(response, models)
    response["retrieval_metrics"]["embedding"]["backend"] = "hashed-local"
    assert not assess_backends(response, models)


def test_values_normalize_format_without_changing_date_or_amount():
    assert normalize("2026年03月18日 128,000元") == normalize("2026/3/18 128000元")
    assert normalize("2026-03-18") != normalize("2026-03-19")
    assert p95(list(range(1, 13))) == 12


def test_numeric_values_dates_and_identifiers_need_complete_boundaries():
    for text, value in (("本金128,000元", "128000"), ("本金12.8万元", "12.8万"),
                        ("本金十二万八千元", "十二万八千"), ("日期2026年03月18日", "2026/3/18"),
                        ("合同HT-2026-071。", "HT-2026-071")):
        assert known_value_in_text(text, value), (text, value)
    for text, value in (("本金1280000元", "128000"), ("本金1128000元", "128000"),
                        ("本金128000.5元", "128000"), ("本金128000万元", "128000"),
                        ("本金-128000元", "128000"), ("本金−128000元", "128000"),
                        ("本金负128000元", "128000"), ("本金12.8万5千元", "12.8万"),
                        ("本金一十二万八千元", "十二万八千"), ("日期2026年3月180日", "2026/3/18"),
                        ("日期12026/3/18", "2026/3/18"), ("编号XHT-2026-071", "HT-2026-071"),
                        ("编号HT-2026-0710", "HT-2026-071"), ("编号HT-2026-071-A", "HT-2026-071")):
        assert not known_value_in_text(text, value), (text, value)


def test_changed_digit_or_obvious_numeric_negation_cannot_pass_as_the_known_amount():
    item, sources, documents, response = fixture()
    for text in ("本金1280000元[资料1]。", "本金-128000元[资料1]。", "本金不是128000元[资料1]。",
                 "本金并非：128000元[资料1]。"):
        changed = deepcopy(response)
        changed["answer"] = text
        assert not assess_answer(item, changed, documents, sources)["passed"], text
    response["citations"][0]["quote"] = "借款本金1280000元。"
    assert not assess_answer(item, response, documents, sources)["passed"]


def test_explicitly_negated_wrong_amount_is_not_an_affirmed_forbidden_value():
    item, sources, documents, response = fixture()
    item["forbidden"] = ["130000"]
    response["answer"] = "本金128000元，并非130000元[资料1]。"
    assert assess_answer(item, response, documents, sources)["passed"]


def test_automatic_value_checks_never_claim_to_validate_extra_legal_opinions():
    item, sources, documents, response = fixture()
    response["answer"] += "合同通常具有更高证明力。"
    assessment = assess_answer(item, response, documents, sources)
    # This additional legal statement is outside the frozen value contract and
    # must remain visible for independent manual review, even if tokens pass.
    assert assessment["passed"]
    assert not assessment["semantic_entailment_checked"]
    assert assessment["manual_review_required"]
