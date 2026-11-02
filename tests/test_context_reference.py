"""What makes a question point back at an earlier turn, and which documents it names.

Both cases come from the first model gate run (2026-11-02): a pronoun whose
antecedent sits in the same sentence triggered a needless clarification, and
"宣传邮件" did not name the email document, so retrieval skipped its pages.
"""
import pytest

from app.context import refers_to_earlier_turn, resolve_question

DOCUMENTS = [
    {"id": 1, "name": "01_起诉意见书.txt"},
    {"id": 2, "name": "02_张某询问笔录.txt"},
    {"id": 3, "name": "03_李某询问笔录.txt"},
    {"id": 5, "name": "05_电子邮件与宣传材料.txt"},
]


@pytest.mark.parametrize("question", [
    "谁最后确认收益率：请区分张某和李某各自的陈述，不能替其定案。",
    "张某说他没有看过宣传页，他们之间谁在说谎？",
    "其中张某的陈述有几处前后不一？",
])
def test_a_pronoun_after_a_named_party_stays_in_the_sentence(question):
    assert not refers_to_earlier_turn(question)


@pytest.mark.parametrize("question", [
    "他后来怎么说？",
    "其陈述与张某是否一致？",  # the pronoun comes first: its referent is earlier
    "张某提到的这份合同签了吗？",  # demonstratives are never exempted
    "上述金额里张某经手多少？",
])
def test_references_to_earlier_turns_are_still_detected(question):
    assert refers_to_earlier_turn(question)


def test_named_parties_do_not_trigger_a_clarification_without_history():
    resolution = resolve_question(1, "谁最后确认收益率：请区分张某和李某各自的陈述，不能替其定案。",
                                  documents=DOCUMENTS)
    assert resolution.status == "explicit"


def test_an_email_named_casually_still_selects_the_email_document():
    resolution = resolve_question(1, "笔录中的固定回报执行记录与宣传邮件的到期支付承诺有哪些可核对措辞？",
                                  documents=DOCUMENTS)
    assert set(resolution.object_document_ids) == {2, 3, 5}


def test_narrowing_by_type_word_keeps_the_named_party_in_scope():
    # "邮件" alone would narrow retrieval to the email and drop 张某's own statement.
    resolution = resolve_question(1, "张某否认看到保本保息，与邮件的固定回报要求应怎样并列核对？", documents=DOCUMENTS)
    assert set(resolution.object_document_ids) == {2, 5}
