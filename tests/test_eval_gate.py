"""The answer-quality gate passes the current code and blocks a real regression."""
from __future__ import annotations

import copy
import json

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
