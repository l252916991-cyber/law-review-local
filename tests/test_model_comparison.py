from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from app.db import connect
from app.model_comparison import compare_local_models
from app.local_backends import BackendUnknown
from app.runtime_ports import AccessDeniedError, ContractValidationError
from app.security import Principal, _principal
from tests.test_domain_writes import domain  # noqa: F401
from tests.test_host_writes import isolated  # noqa: F401
from tests.test_local_backends import local_models  # noqa: F401

actual = pytest.mark.skipif(not os.getenv("LAW_REVIEW_FORGE_PYTHON"), reason="installed framework Python3.12 required")


@pytest.mark.parametrize("filename", ["services.py", "agents.py", "review_collaboration.py", "agent_tools.py"])
def test_native_baseline_still_refuses_any_unreviewed_source_change(filename):
    from app.model_comparison import native_baseline_identity

    original = Path.read_bytes

    def changed(path):
        value = original(path)
        return value + b"# unreviewed change" if path.name == filename else value

    with patch.object(Path, "read_bytes", new=changed), pytest.raises(ContractValidationError, match="pinned baseline"):
        native_baseline_identity()


def test_native_baseline_identifies_reviewed_snapshot_without_claiming_an_old_commit_matches():
    from app.model_comparison import native_baseline_identity

    identity = native_baseline_identity()
    assert identity["schema_version"] == 2
    assert identity["revision"] == "specialist-review-v3"
    # A re-registered baseline must carry its review note, not just new hashes.
    assert "PROMPT_VERSION" in identity["review_note"]
    assert identity["source_commit"] == "c63abf953bf10f6f025c2ff22e5819247590e58a"
    assert "commit" not in identity
    assert "review_collaboration.py" in identity["algorithm_files"]


@actual
@pytest.mark.parametrize("case_id,question,expected", [
    (1, "合成审批记录的事实和证据是否完整，有哪些疏漏？", "success"),
    (2, "量子香蕉完全无关资料", "source_rejection"),
])
def test_paired_models_keep_source_unchanged_and_separate_measurements(local_models, tmp_path, case_id, question, expected):
    models, calls = local_models
    with closing(connect()) as conn:
        before = tuple(conn.iterdump())
    output = tmp_path / "comparison"
    report = compare_local_models(case_id=case_id, question=question, models=models,
        framework_python=os.environ["LAW_REVIEW_FORGE_PYTHON"], output=output, expected=expected)
    assert report["engineering_passed"], json.dumps(report, ensure_ascii=False, indent=2)
    assert report["source_unchanged"] and not report["g2_accepted"] and not report["measurement_valid"]
    assert report["candidate"]["status"] == ("succeeded" if expected == "success" else "failed")
    assert report["candidate_usage"]["calls"] >= 3 and report["candidate_usage"]["unknown_usage"] == 0
    assert report["candidate_decision"] == ("supported" if expected == "success" else "unrelated")
    # Content acceptance must judge the model's own sentences, not the host quotes.
    assert report["candidate_answer_body"] == ("合成审批记录需核验。" if expected == "success" else "")
    assert "[资料" not in report["candidate_answer_body"]
    assert "请律师复核" not in report["candidate_answer_body"]
    assert report["candidate_findings"] == ([{"text": "合成审批记录需核验。", "sources": [1]}] if expected == "success" else [])
    if expected == "success":
        assert "[资料1]" in report["candidate_answer"]
        assert report["candidate_answer_body"] in report["candidate_answer"]
    else:
        assert not report["candidate_answer"] and not report["candidate_findings"]
    shared, independent = report["comparisons"]
    assert shared["mode"] == "shared_retrieval_and_memory_snapshot"
    assert independent["mode"] == "independent_retrieval_and_memory"
    assert shared["native_usage"]["calls"] == 1
    assert independent["native_usage"]["calls"] > shared["native_usage"]["calls"]
    assert all(c["source_business_writes"] == 0 for c in report["comparisons"])
    assert all(not c["model_quality_assessed"] for c in report["comparisons"])
    with closing(connect()) as conn:
        assert tuple(conn.iterdump()) == before
    assert (output / "report.json").stat().st_mode & 0o777 == 0o600
    observed = list(calls)
    with pytest.raises(FileExistsError):
        compare_local_models(case_id=1, question=question, models=models,
            framework_python=os.environ["LAW_REVIEW_FORGE_PYTHON"], output=output, expected=expected)
    assert calls == observed


@actual
def test_unrelated_nonempty_dense_retrieval_is_rejected_without_hiding_native_error(local_models, tmp_path):
    # Preserve the original false-positive input and constant dense vectors.
    # An explicit measured refusal, not an empty retrieval, closes this case.
    models, _ = local_models
    report = compare_local_models(case_id=1, question="量子香蕉完全无关资料", models=models,
        framework_python=os.environ["LAW_REVIEW_FORGE_PYTHON"], output=tmp_path / "negative-control",
        expected="source_rejection")
    assert report["candidate"]["status"] == "failed"
    assert report["candidate_business_outcome"] == "source_rejection"
    assert report["engineering_passed"] and not report["g2_accepted"]
    assert report["candidate_refusal"]["decision"] == "unrelated"
    assert all(c["checks"]["expected_outcome"] for c in report["comparisons"])
    assert all(c["native_baseline_negative_accepted"] for c in report["comparisons"])


@actual
@pytest.mark.parametrize("fault", ["disconnect", "missing_usage"])
def test_unknown_native_usage_stops_before_another_comparison(local_models, tmp_path, fault):
    models, calls = local_models
    original = models.transport
    llm_calls = 0
    def unknown_native(kind, payload, timeout):
        nonlocal llm_calls
        if kind == "llm":
            llm_calls += 1
            if llm_calls == 2 and fault == "disconnect":
                calls.append("llm")
                raise OSError("synthetic lost response")
        body = original(kind, payload, timeout)
        if kind == "llm" and llm_calls == 2:
            body.pop("usage")
        return body
    models.transport = unknown_native
    output = tmp_path / "unknown-native"
    with closing(connect()) as conn:
        before = tuple(conn.iterdump())
    with pytest.raises(BackendUnknown, match="evidence retained"):
        compare_local_models(case_id=1, question="合成审批记录", models=models,
            framework_python=os.environ["LAW_REVIEW_FORGE_PYTHON"], output=output)
    assert llm_calls == 2 and not (output / "independent_retrieval_and_memory").exists()
    failure = json.loads((output / "failure.json").read_text())
    assert failure["outcome"] == "unknown_usage" and failure["provider_calls_not_retried"]
    assert sum(not e["usage_known"] for e in failure["native_backend_calls"]) == 1
    with closing(connect()) as conn:
        assert tuple(conn.iterdump()) == before


def test_comparison_does_not_escalate_a_view_only_actor(local_models, tmp_path, monkeypatch):
    models, calls = local_models
    token = "comparison-view-token-" + "x" * 32
    monkeypatch.setenv("LAW_REVIEW_AUTH_MODE", "token")
    monkeypatch.setenv("LAW_REVIEW_API_TOKENS_JSON", json.dumps({token: {
        "name": "只读评测人", "case_ids": [1], "permissions": ["view"]}}))
    marker = _principal.set(Principal("只读评测人", False, (1,), permissions=frozenset({"view"})))
    try:
        with pytest.raises(AccessDeniedError):
            compare_local_models(case_id=1, question="合成审批记录", models=models,
                framework_python="/unneeded", output=tmp_path / "view-only")
    finally:
        _principal.reset(marker)
    assert not calls


@actual
@pytest.mark.parametrize("content", ["not JSON", json.dumps({"decision": "unrelated", "reason": "no source",
    "findings": [{"text": "仍然给出结论", "sources": [1]}]})])
def test_system_failure_is_not_counted_as_a_business_refusal(local_models, tmp_path, content):
    models, _ = local_models
    original = models.transport
    def malformed(kind, payload, timeout):
        body = original(kind, payload, timeout)
        if kind == "llm" and "candidate-answerability-" in payload["messages"][0]["content"]:
            body["choices"][0]["message"]["content"] = content
        return body
    models.transport = malformed
    report = compare_local_models(case_id=1, question="量子香蕉完全无关资料", models=models,
        framework_python=os.environ["LAW_REVIEW_FORGE_PYTHON"], output=tmp_path / "broken-negative",
        expected="source_rejection")
    assert report["candidate"]["status"] == "failed"
    assert report["candidate_business_outcome"] == "system_failure"
    assert not report["engineering_passed"] and report["candidate_refusal"] is None


def test_native_algorithm_drift_is_rejected_before_creating_run(local_models, tmp_path, monkeypatch):
    from app import model_comparison
    models, calls = local_models
    output = tmp_path / "uncreated"
    monkeypatch.setitem(model_comparison.NATIVE_ALGORITHM_HASHES, "agents.py", "0" * 64)
    with pytest.raises(ContractValidationError, match="pinned baseline"):
        compare_local_models(case_id=1, question="合成审批记录", models=models,
            framework_python="/unused", output=output)
    assert not output.exists() and not calls
