"""Paired ablation analysis: denominators, pairing, McNemar, case bootstrap."""
from __future__ import annotations

import json

import pytest

from app.ablation_analysis import (
    analyse,
    case_bootstrap,
    load_attempts,
    mcnemar_exact,
    pair_attempts,
    repeat_variation,
)
from scripts.analyse_collaboration_ablation import main


def row(item: str, group: str, passed: bool, **overrides):
    base = {
        "item": item, "case_id": 1, "category": "normal", "group": group, "repeat": 0,
        "outcome": "ok", "executed": True, "passed": passed, "usage_known": True,
        "input_tokens": 100, "output_tokens": 20, "elapsed_seconds": 1.0,
    }
    base.update(overrides)
    return base


def ledger(*rows) -> list[dict]:
    return list(rows)


def test_pairing_reports_an_unpaired_item_instead_of_dropping_it():
    rows = ledger(row("i1", "A", True), row("i1", "C", True),
                  row("i2", "A", False), row("i2", "C", True),
                  row("i3", "A", True))
    pairs, unpaired = pair_attempts(rows, "A", "C")
    assert len(pairs) == 2
    assert unpaired == [{"item": "i3", "repeat": 0, "present": ["A"], "missing": ["C"]}]


def test_failed_and_refused_attempts_stay_in_the_paired_denominator():
    rows = ledger(row("i1", "A", True),
                  row("i1", "C", False, outcome="transport_error", executed=True),
                  row("i2", "A", True), row("i2", "C", True))
    report = analyse(rows, "A", "C", iterations=200)
    summary = report["summary"]
    assert summary["pairs"] == 2
    assert summary["baseline"]["passed"] == 2 and summary["compare"]["passed"] == 1
    assert summary["pass_rate_baseline"] == 1.0 and summary["pass_rate_compare"] == 0.5
    assert summary["delta_pass_rate"] == -0.5
    assert summary["compare"]["failed_or_refused"] == 1


def test_mcnemar_exact_matches_the_binomial_table():
    assert mcnemar_exact(0, 0)["p_value"] is None
    assert mcnemar_exact(1, 6)["p_value"] == pytest.approx(2 * (1 + 7) / 128)
    assert mcnemar_exact(3, 3)["p_value"] == 1.0


def test_per_category_and_per_case_splits_keep_their_own_counts():
    rows = ledger(
        row("i1", "A", False, category="normal", case_id=1),
        row("i1", "C", True, category="normal", case_id=1),
        row("i2", "A", True, category="contradiction", case_id=2),
        row("i2", "C", False, category="contradiction", case_id=2),
    )
    report = analyse(rows, "A", "C", iterations=200)
    assert report["by_category"]["normal"]["delta_pass_rate"] == 1.0
    assert report["by_category"]["contradiction"]["delta_pass_rate"] == -1.0
    assert report["by_case"]["1"]["delta_pass_rate"] == 1.0
    assert report["by_case"]["2"]["delta_pass_rate"] == -1.0


def test_repeat_variation_reports_flips_not_averaged_away():
    rows = ledger(row("i1", "C", True, repeat=0), row("i1", "C", False, repeat=1),
                  row("i2", "C", True, repeat=0), row("i2", "C", True, repeat=1),
                  row("i1", "A", True, repeat=0), row("i1", "A", True, repeat=1))
    assert repeat_variation(rows, "C") == {
        "group": "C", "items": 2, "items_with_flips": 1, "max_repeats": 2, "stable_items": 1}
    assert repeat_variation(rows, "A")["items_with_flips"] == 0


def test_case_bootstrap_is_seeded_bounded_and_keeps_the_case_count_visible():
    rows = ledger(
        row("i1", "A", False, case_id=1), row("i1", "C", True, case_id=1),
        row("i2", "A", False, case_id=2), row("i2", "C", True, case_id=2),
        row("i3", "A", True, case_id=2), row("i3", "C", False, case_id=2),
    )
    pairs, _ = pair_attempts(rows, "A", "C")
    first = case_bootstrap(pairs, iterations=500, seed=7)
    second = case_bootstrap(pairs, iterations=500, seed=7)
    assert first == second
    assert first["cases"] == 2 and first["iterations"] == 500
    low, high = first["interval"]
    assert -1.0 <= low <= high <= 1.0
    assert "不能当作总体估计" in first["note"]


def test_missing_case_id_is_reported_rather_than_bootstrapped():
    rows = ledger(row("i1", "A", True, case_id=None), row("i1", "C", True, case_id=None))
    report = analyse(rows, "A", "C", iterations=200)
    assert report["case_bootstrap"]["cases"] == 0
    assert report["case_bootstrap"]["interval"] is None


def test_cost_counts_known_and_unknown_usage_separately():
    rows = ledger(
        row("i1", "A", True, input_tokens=100, output_tokens=10, elapsed_seconds=1.0),
        row("i1", "C", True, input_tokens=400, output_tokens=30, elapsed_seconds=2.5),
        row("i2", "A", True, usage_known=False, input_tokens=None, output_tokens=None, elapsed_seconds=None),
        row("i2", "C", True, usage_known=False, input_tokens=None, output_tokens=None, elapsed_seconds=None),
    )
    report = analyse(rows, "A", "C", iterations=200)
    cost = report["cost"]
    assert cost["input_tokens"]["known_pairs"] == 1
    assert cost["input_tokens"]["unknown_pairs"] == 1
    assert cost["input_tokens"]["median_delta"] == 300
    assert cost["output_tokens"]["total_baseline"] == 10
    assert cost["elapsed_seconds"]["median_delta"] == 1.5
    assert cost["unknown_usage_pairs"] == 1


def test_ledger_validation_rejects_malformed_rows(tmp_path):
    good = tmp_path / "attempts.jsonl"
    good.write_text(json.dumps(row("i1", "A", True)) + "\n", encoding="utf-8")
    assert len(load_attempts(good)) == 1
    for content, message in (
        ("{not json}\n", "不是合法 JSON"),
        (json.dumps({"item": "i1", "group": "A"}) + "\n", "缺少必需字段"),
        (json.dumps(row("i1", "D", True)) + "\n", "分组未注册"),
        ("\n", "账本为空"),
    ):
        path = tmp_path / f"bad-{abs(hash(content))}.jsonl"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_attempts(path)


def test_cli_writes_once_and_refuses_overwrite(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    rows = [row("i1", "A", True), row("i1", "C", True), row("i2", "A", False), row("i2", "C", True)]
    (run / "attempts.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                        encoding="utf-8")
    output = tmp_path / "report.json"
    assert main(["--run", str(run), "--baseline", "A", "--compare", "C", "--iterations", "200",
                 "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["paired"] == 2 and report["summary"]["delta_pass_rate"] == 0.5
    assert report["source"].endswith("attempts.jsonl")
    assert main(["--run", str(run), "--baseline", "A", "--compare", "C", "--iterations", "200",
                 "--output", str(output)]) != 0
    with pytest.raises(SystemExit):
        main(["--run", str(run), "--baseline", "A", "--compare", "A", "--iterations", "200",
              "--output", str(tmp_path / "other.json")])
    assert not (tmp_path / "other.json").exists()


def test_outcome_counts_keep_unattempted_and_unknown_usage_visible():
    rows = ledger(
        row("i1", "A", True),
        row("i1", "C", True, usage_known=False, input_tokens=None, output_tokens=None),
        row("i2", "A", False, outcome="not_attempted", executed=False, rule_refused=True),
        row("i2", "C", False, outcome="timeout", executed=True),
        row("i3", "C", False, outcome="contract_failed", executed=True),
    )
    report = analyse(rows, "A", "C", iterations=200)
    assert report["outcomes"]["A"] == {"attempts": 2, "outcomes": {"not_attempted": 1, "ok": 1},
                                       "not_attempted": 1, "unknown_usage": 0}
    assert report["outcomes"]["C"]["unknown_usage"] == 1
    assert report["outcomes"]["C"]["not_attempted"] == 0
    assert report["outcomes"]["C"]["outcomes"]["timeout"] == 1


def test_failure_by_category_separates_failure_kinds():
    rows = ledger(
        row("i1", "A", True, category="normal"),
        row("i1", "C", False, category="normal", outcome="transport_error"),
        row("i2", "A", False, category="contradiction", outcome="timeout"),
        row("i2", "C", True, category="contradiction"),
        row("i3", "C", False, category="contradiction", outcome="contract_failed"),
    )
    report = analyse(rows, "A", "C", iterations=200)
    assert report["failure_by_category"]["A"] == {"contradiction": {"timeout": 1}}
    assert report["failure_by_category"]["C"] == {
        "contradiction": {"contract_failed": 1}, "normal": {"transport_error": 1}}


def test_rule_degradation_note_and_mapping_are_explicit():
    rows = ledger(row("i1", "A", True), row("i1", "C", False, outcome="model_unavailable"))
    report = analyse(rows, "A", "C", iterations=200)
    assert any("规则降级" in note for note in report["notes"])
    assert report["outcomes"]["C"]["outcomes"] == {"model_unavailable": 1}
    assert report["summary"]["pass_rate_compare"] == 0.0


def test_cli_accepts_attempts_without_a_run_directory(tmp_path):
    attempts = tmp_path / "attempts.jsonl"
    rows = [row("i1", "A", True), row("i1", "C", True)]
    attempts.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    output = tmp_path / "report.json"
    assert main(["--attempts", str(attempts), "--baseline", "A", "--compare", "C",
                 "--iterations", "200", "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["paired"] == 1 and report["run"] is None
    with pytest.raises(SystemExit):
        main(["--baseline", "A", "--compare", "C", "--output", str(tmp_path / "none.json")])
