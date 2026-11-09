"""The answer contract's lawyer-review check: a waiver of review rejects the answer,
a description of the case that happens to use the same words does not."""
import pytest

from app.agents import answer_contract

REMINDER = "请律师复核原文与上述结论。"


@pytest.mark.parametrize("waiver", [
    "结论[资料1]，无需律师复核。",
    "结论[资料1]。律师不必再核验。",
    "结论[资料1]。以上结论无需复核。",
    "结论[资料1]，不用复核，可直接引用。",
    "结论[资料1]。本结论无须人工核验",
])
def test_an_answer_that_waives_review_is_rejected(waiver):
    assert not answer_contract(f"{waiver}{REMINDER}", 1)["lawyer_review_present"]


@pytest.mark.parametrize("answer", [
    # Qwythos-9B, 2026-11-08, contradiction-04: the waiver is about checking the clients.
    "熟人客户登记：注明客户来源为“熟人介绍”，无需核验特定身份[资料1]。",
    "李某称老客户转发二维码后任何人都可以登记，公司认为无需核对投资人资格[资料1]。",
])
def test_a_case_fact_using_the_same_words_is_not_a_waiver(answer):
    checks = answer_contract(f"{answer}{REMINDER}", 1)
    assert checks["lawyer_review_present"] and checks["citations_valid"]


def test_the_reminder_is_still_required():
    assert not answer_contract("熟人客户无需核验特定身份[资料1]。", 1)["lawyer_review_present"]
