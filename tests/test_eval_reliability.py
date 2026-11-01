"""Ledger reliability analysis keeps independent units, missing usage and failures visible."""
import hashlib
import json
from copy import deepcopy

import pytest

from scripts.analyse_eval_reliability import analyse, main, metric


def row(item="q1", repeat=0, **extra):
    value = {"item": item, "group": "A", "case_id": 1, "category": "normal", "repeat": repeat,
             "passed": True, "executed": True, "outcome": "ok", "raw_answer": "合成回答",
             "input_tokens": 100, "output_tokens": 20, "latency_ms": 800, "elapsed_seconds": 1.0}
    value.update(extra)
    return value


def test_failures_stay_in_success_denominator_and_executed_cost():
    rows = [row(repeat=i, passed=i != 1, outcome="contract_failed" if i == 1 else "ok") for i in range(3)]
    rows += [row("q2", i, case_id=2) for i in range(3)]
    rows += [row("q3", i, case_id=2, executed=False, passed=False, outcome="not_attempted", raw_answer=None,
                 input_tokens=None, output_tokens=None, latency_ms=None, elapsed_seconds=None) for i in range(3)]
    result = analyse(rows, iterations=200)
    group = result["groups"]["A"]
    assert result["unique_question_ids"] == 3 and result["case_clusters"] == 2
    assert group["planned_recorded_attempts"] == 9 and group["executed_attempts"] == 6
    assert group["passed_attempts"] == 5 and group["recorded_pass_rate"] == pytest.approx(5 / 9)
    assert group["questions_with_requested_repeats"] == 0
    assert group["questions_with_repeated_model_answers"] == 2
    assert group["questions_without_generated_answers"] == 1
    assert group["questions_with_pass_flips"] == 1 and group["questions_with_outcome_changes"] == 1
    assert group["cost_all_recorded_attempts"]["total_tokens"]["complete_sum"] is None
    assert group["cost_all_recorded_attempts"]["total_tokens"]["missing_records"] == 3
    assert group["cost_executed_including_failures"]["total_tokens"]["complete_sum"] == 720
    assert group["executed_tokens_per_passed_attempt"] == 144
    assert group["per_question"][0]["recorded_pass_variance"] == pytest.approx(2 / 9)
    interval = group["case_uncertainty"]
    assert interval["case_clusters"] == 2 and interval["independent_question_ids"] == 3
    assert interval["population_inference_validated"] is False
    assert result["semantic_consistency_measured"] is False and result["human_agreement_measured"] is False


def test_single_sample_is_not_reported_as_repeat_consistency_or_case_ci():
    group = analyse([row()], iterations=100)["groups"]["A"]
    assert group["questions_with_two_or_more_records"] == 0
    assert group["per_question"][0]["recorded_pass_stable"] is None
    assert group["per_question"][0]["recorded_pass_variance"] is None
    assert group["case_uncertainty"]["ci95"] is None
    assert group["case_uncertainty"]["reason"] == "fewer_than_two_case_clusters"


def test_missing_usage_is_null_while_recorded_zero_remains_a_known_value():
    assert metric([None, None])["known_sum"] is None
    assert metric([0.0, None])["known_sum"] == 0
    assert metric([0.0, None])["complete_sum"] is None
    assert metric([0.0])["complete_sum"] == 0
    result = analyse([row(input_tokens=None)], iterations=100)["groups"]["A"]
    assert result["executed_tokens_per_passed_attempt"] is None


def test_five_records_with_wrong_repeat_identity_do_not_qualify_as_five_requested_repeats():
    rows = [row(repeat=i) for i in (1, 2, 3, 4, 5)]
    assert analyse(rows, iterations=100)["groups"]["A"]["questions_with_requested_repeats"] == 0
    rows = [row(repeat=i) for i in range(5)]
    assert analyse(rows, iterations=100)["groups"]["A"]["questions_with_requested_repeats"] == 1


def test_duplicate_and_changed_case_metadata_fail_closed():
    with pytest.raises(ValueError, match="Duplicate"):
        analyse([row(), row()])
    with pytest.raises(ValueError, match="case/category"):
        analyse([row(), row(repeat=1, case_id=2)])
    with pytest.raises(ValueError, match="booleans"):
        analyse([row(passed=1)])


def test_case_bootstrap_is_seeded_and_does_not_expand_n_with_repeats():
    rows = [row(repeat=i, passed=i != 1) for i in range(3)]
    rows += [row("q2", i, case_id=2) for i in range(3)]
    first = analyse(rows, iterations=200, seed=8)
    second = analyse(list(reversed(deepcopy(rows))), iterations=200, seed=8)
    assert first == second
    assert first["groups"]["A"]["case_uncertainty"]["case_clusters"] == 2


@pytest.mark.parametrize("changes", [
    {"executed": False}, {"raw_answer": ""}, {"raw_answer": None},
    {"outcome": "timeout"}, {"outcome": "contract_failed"},
])
def test_inconsistent_success_records_cannot_inflate_passes_or_reduce_cost(changes):
    with pytest.raises(ValueError, match="Inconsistent"):
        analyse([row(**changes), row(repeat=1)], iterations=100)


def test_cli_binds_source_and_refuses_existing_output(tmp_path):
    source, output = tmp_path / "attempts.jsonl", tmp_path / "report.json"
    source.write_text(json.dumps(row()) + "\n")
    args = ["--attempts", str(source), "--output", str(output), "--iterations", "100"]
    assert main(args) == 0
    result = json.loads(output.read_text())
    assert result["source_ledger_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    before = output.read_bytes()
    assert main(args) == 2 and output.read_bytes() == before
