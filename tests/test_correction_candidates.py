from unittest.mock import patch

import pytest

from app.correction_candidates import apply_edits, predict


def test_patch_preserves_unedited_text():
    assert apply_edits('甲错字，100元。', '[{"old":"错字","new":"正字"}]') == '甲正字，100元。'
    assert apply_edits('甲甲', '[]') == '甲甲'


@pytest.mark.parametrize('answer', ['{}', '[{"old":"甲","new":"乙"}]', '[{"old":"甲甲","new":"乙"},{"old":"甲甲","new":"丙"}]'])
def test_rejects_ambiguous_or_overlapping_edits(answer):
    with pytest.raises(ValueError):
        apply_edits('甲甲', answer)


def test_failed_first_call_stops_review():
    with patch('app.correction_candidates._call', return_value={'error':'failure','prediction':''}) as call:
        result = predict('review', '说明', '原文', {})
    assert result['error'] == 'failure'
    assert call.call_count == 1


def test_metadata_cannot_enter_request():
    with patch('app.correction_candidates._call', return_value={'error':None,'prediction':'原文'}) as call:
        result = predict('ensemble', '说明', '原文', {'reference':'SECRET_GOLD'})
    assert 'SECRET_GOLD' not in str(call.call_args_list)
    assert len(result['calls']) == 3
