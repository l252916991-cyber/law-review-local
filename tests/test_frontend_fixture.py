"""Keep browser test model scenarios independent across browser projects."""
from scripts.acceptance.stub_model import CHAT_MODEL, FALLBACK_MODEL, INVALID_ANSWER, VALID_ANSWER, StubModel


def test_repeatable_fallback_rejects_each_primary_and_accepts_only_fallback():
    model = StubModel(repeatable_fallback=True)
    for _ in range(2):
        assert model._pick_answer("回退验证", CHAT_MODEL) == INVALID_ANSWER
        assert model._pick_answer("回退验证", FALLBACK_MODEL) == VALID_ANSWER


def test_default_fixture_preserves_single_failure_and_retraction_behavior():
    model = StubModel()
    assert model._pick_answer("回退验证") == INVALID_ANSWER
    assert model._pick_answer("回退验证") == VALID_ANSWER
    assert model._pick_answer("拒答校验", FALLBACK_MODEL) == INVALID_ANSWER
