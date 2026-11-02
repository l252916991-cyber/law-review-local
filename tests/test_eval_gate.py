"""The answer-quality gate passes the current code and blocks a real regression."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app import eval_gate
from app.eval_gate import (
    DEFAULT_BASELINES,
    GateConfigurationError,
    baseline_from,
    compare,
    render_markdown,
    run,
)

BASELINE = json.loads(DEFAULT_BASELINES["rules"].read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def current(tmp_path_factory):
    return run("rules", workdir=tmp_path_factory.mktemp("gate"))


def test_committed_baseline_is_a_registered_rules_baseline():
    assert BASELINE["manifest"]["mode"] == "rules"
    assert BASELINE["reason"]
    assert len(BASELINE["items"]) == BASELINE["summary"]["queries"] == 60


def test_current_code_passes_the_committed_baseline(current):
    verdict = compare(current, BASELINE)
    assert verdict["passed"], verdict["failures"]
    assert verdict["regressions"] == []
    # Offline answers never call a model.
    assert current["summary"]["llm_used"] == 0


def test_a_retrieval_regression_is_blocked(tmp_path, monkeypatch):
    from app.rag import HybridRetriever

    original = HybridRetriever.retrieve

    def top_one(self, query, limit=6):
        contexts, metrics = original(self, query, limit)
        return contexts[:1], metrics

    # A plausible "tuning" mistake: keep only the best page.
    monkeypatch.setattr(HybridRetriever, "retrieve", top_one)
    degraded = run("rules", workdir=tmp_path)
    verdict = compare(degraded, BASELINE)
    assert not verdict["passed"]
    assert verdict["regressions"], "the gate must name the questions that regressed"
    assert any("通过题数" in text or "必需页" in text for text in verdict["failures"])
    markdown = render_markdown(degraded, verdict, BASELINE)
    assert "未通过" in markdown and verdict["regressions"][0] in markdown


def _synthetic(mode: str, *, passed: int, p95: float, violations: int = 0, refusals: int = 6,
               required: int = 38, inference_id: str = "a") -> dict:
    items = {f"q{i:02d}": {"passed": i < passed} for i in range(60)}
    return {
        "manifest": {"mode": mode, "inference_id": inference_id, "dataset": {"sha256": "d"}},
        "summary": {"queries": 60, "passed": passed, "refusal_correct": refusals,
                    "required_pages_cited": required, "violation_count": violations,
                    "latency_p50_ms": p95 / 2, "latency_p95_ms": p95},
        "items": items,
    }


def test_model_mode_tolerates_two_questions_of_sampling_noise():
    base = _synthetic("model", passed=50, p95=4000, inference_id="old")
    assert compare(_synthetic("model", passed=48, p95=4000, inference_id="new"), base)["passed"]
    verdict = compare(_synthetic("model", passed=47, p95=4000, inference_id="new"), base)
    assert not verdict["passed"] and "通过题数" in verdict["failures"][0]


def test_latency_is_gated_in_model_mode_only_above_the_noise_floor():
    slow = compare(_synthetic("model", passed=50, p95=5100, inference_id="n"),
                   _synthetic("model", passed=50, p95=4000, inference_id="o"))
    assert not slow["passed"] and "p95" in slow["failures"][0]
    fast_base = _synthetic("model", passed=50, p95=100, inference_id="o")
    assert compare(_synthetic("model", passed=50, p95=190, inference_id="n"), fast_base)["passed"]
    # Rules mode never fails on wall-clock time: CI machines differ.
    assert compare(_synthetic("rules", passed=50, p95=9000, inference_id="n"),
                   _synthetic("rules", passed=50, p95=10, inference_id="o"))["passed"]


def test_new_forbidden_claims_fail_in_every_mode():
    for mode in ("rules", "model"):
        verdict = compare(_synthetic(mode, passed=50, p95=10, violations=1, inference_id="n"),
                          _synthetic(mode, passed=50, p95=10, inference_id="o"))
        assert not verdict["passed"]


def test_identical_configuration_with_different_results_is_nondeterminism():
    base = _synthetic("rules", passed=50, p95=10, inference_id="same")
    moved = copy.deepcopy(base)
    moved["items"]["q00"]["passed"], moved["items"]["q55"]["passed"] = False, True
    verdict = compare(moved, base)
    assert not verdict["passed"]
    assert any("不确定性" in text for text in verdict["failures"])


@pytest.mark.parametrize("field,value", [("mode", "model"), ("dataset", {"sha256": "other"})])
def test_incomparable_baselines_are_configuration_errors(field, value):
    base = _synthetic("rules", passed=50, p95=10)
    run_ = copy.deepcopy(base)
    run_["manifest"][field] = value
    with pytest.raises(GateConfigurationError):
        compare(run_, base)


def test_registering_a_baseline_needs_a_reason_and_keeps_history(current):
    with pytest.raises(GateConfigurationError):
        baseline_from(current, reason="  ", previous=BASELINE)
    new = baseline_from(current, reason="测试登记", previous=BASELINE)
    assert new["reason"] == "测试登记"
    assert new["history"][-1]["inference_id"] == BASELINE["manifest"]["inference_id"]
    assert set(new["items"]) == set(current["items"])


def test_manifest_names_the_inference_configuration(current):
    manifest = current["manifest"]
    assert manifest["gate_version"] == eval_gate.GATE_VERSION
    assert set(manifest["modules"]) == set(eval_gate.INFERENCE_MODULES)
    assert manifest["prompt_version"] and manifest["system_prompt_sha256"]
    assert len(manifest["inference_id"]) == 16


def test_cli_exit_codes(tmp_path, capsys):
    from scripts.eval_gate import main

    assert main(["--baseline", str(tmp_path / "missing.json")]) == 2
    # A baseline in which one currently failing question used to pass.
    stricter = copy.deepcopy(BASELINE)
    failing = next(key for key, row in stricter["items"].items() if not row["passed"])
    stricter["items"][failing]["passed"] = True
    stricter["summary"]["passed"] += 1
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(stricter, ensure_ascii=False), encoding="utf-8")
    report = tmp_path / "report.json"
    summary = tmp_path / "summary.md"
    assert main(["--baseline", str(path), "--report", str(report), "--markdown", str(summary)]) == 1
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"]["regressions"] == [failing]
    assert failing in summary.read_text(encoding="utf-8")
    capsys.readouterr()


def test_model_mode_requires_an_endpoint_and_a_model():
    from scripts.eval_gate import main

    with pytest.raises(SystemExit):
        main(["--mode", "model"])


def test_model_mode_that_never_reached_a_model_is_refused(tmp_path):
    # Nothing listens on port 1: every answer falls back to rules.
    with pytest.raises(GateConfigurationError):
        run("model", workdir=tmp_path, endpoint="http://127.0.0.1:1/v1", model="absent")


def test_without_a_baseline_the_summary_never_says_passed(current):
    assert "未比较（没有基线）" in render_markdown(current, None, None)
    assert "：通过" not in render_markdown(current, None, None)
    assert "已登记为基线" in render_markdown(current, None, BASELINE, registered=True)


def test_a_saved_report_is_registered_only_when_the_current_code_would_produce_it(current, tmp_path, capsys):
    from scripts.eval_gate import main

    def register(report: dict) -> tuple[int, Path]:
        source = tmp_path / f"report-{len(list(tmp_path.iterdir()))}.json"
        source.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        target = tmp_path / f"baseline-{source.stem}.json"
        code = main(["--update-baseline", "--from-report", str(source), "--baseline", str(target),
                     "--reason", "登记已复核的报告"])
        return code, target

    code, target = register(current)
    assert code == 0
    registered = json.loads(target.read_text(encoding="utf-8"))
    assert registered["manifest"]["inference_id"] == current["manifest"]["inference_id"]
    assert len(registered["registered_from"]["sha256"]) == 64

    changed_code = copy.deepcopy(current)
    changed_code["manifest"]["modules"]["rag.py"] = "0" * 64
    changed_code["manifest"]["inference_id"] = eval_gate._inference_id(changed_code["manifest"])
    edited_id = copy.deepcopy(current)
    edited_id["manifest"]["prompt_version"] = "other"  # contents no longer match the recorded id
    other_scoring = copy.deepcopy(current)
    other_scoring["manifest"]["gate_version"] = "eval-gate-v0"
    for report in (changed_code, edited_id, other_scoring, {"manifest": {}}):
        code, target = register(report)
        assert code == 2 and not target.exists()
    assert "modules/rag.py" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["--from-report", str(tmp_path / "report-0.json")])


def test_a_baseline_scored_by_another_gate_version_is_not_comparable(current):
    older = copy.deepcopy(current)
    older["manifest"]["gate_version"] = "eval-gate-v1"
    with pytest.raises(GateConfigurationError, match="评分口径不同"):
        compare(current, older)


def test_reports_keep_their_answers_for_rescoring(current):
    assert set(current["answers"]) == set(current["items"])
    assert {"answer", "citations", "answer_status", "llm_used", "latency_ms"} <= set(current["answers"]["normal-01"])


def test_an_older_report_is_rescored_only_if_its_answers_reproduce_it(current, tmp_path, capsys):
    from app.answer_eval import load_dataset, score_answer_v2
    from scripts.eval_gate import main

    # A report as eval-gate-v1 wrote it: v2 scores, no saved answers.
    dataset = load_dataset(eval_gate.DEFAULT_DATASET)
    old = eval_gate.evaluate(dataset, current["answers"], scorer=score_answer_v2)
    old["manifest"] = {**current["manifest"], "gate_version": "eval-gate-v1"}
    old["generated_at"] = current["generated_at"]
    report = tmp_path / "old-report.json"
    report.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    def runner_log(answers: dict) -> Path:
        path = tmp_path / f"answers-{len(list(tmp_path.iterdir()))}.jsonl"
        path.write_text("\n".join(json.dumps({"id": key, "result": value}, ensure_ascii=False)
                                  for key, value in answers.items()), encoding="utf-8")
        return path

    def register(answers_path: Path | None) -> tuple[int, Path]:
        target = tmp_path / f"baseline-{len(list(tmp_path.iterdir()))}.json"
        extra = ["--answers", str(answers_path)] if answers_path else []
        return main(["--update-baseline", "--from-report", str(report), *extra, "--baseline", str(target),
                     "--reason", "按新评分重新登记"]), target

    code, target = register(None)  # nothing to re-score from
    assert code == 2 and not target.exists()

    code, target = register(runner_log(current["answers"]))
    assert code == 0
    registered = json.loads(target.read_text(encoding="utf-8"))
    assert registered["manifest"]["gate_version"] == eval_gate.GATE_VERSION
    assert registered["registered_from"]["rescored"]["from_gate_version"] == "eval-gate-v1"
    assert len(registered["registered_from"]["answers_sha256"]) == 64

    # Answers that are not this run's: one refusal turned into a confident answer.
    altered = copy.deepcopy(current["answers"])
    refusal = next(key for key, row in old["items"].items() if row["refusal_expected"] and row["passed"])
    altered[refusal] = {**altered[refusal], "answer": "结案日期为2025年5月1日。", "answer_status": "answered"}
    code, target = register(runner_log(altered))
    assert code == 2 and not target.exists()
    assert "复现不出" in capsys.readouterr().err
