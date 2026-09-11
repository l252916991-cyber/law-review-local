"""The enhanced runner must record, resume and verify without leaking references."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import scripts.benchmark_enhanced_run as enhanced
import unified_benchmark_runner as runner


def _fake_solve(recorded: list[dict]) -> object:
    def solve(task, instruction, question, config):
        recorded.append({"task": task, "instruction": instruction, "question": question, "config": config})
        call = {"prediction": "[正确答案]A<eoa>", "error": None, "finish_reason": "stop",
                "usage": {"completion_tokens": 5}, "latency_ms": 1.0}
        return {"prediction": "[正确答案]A<eoa>", "error": None, "finish_reason": "stop",
                "usage": call["usage"], "calls": [call], "latency_ms": 1.0, "solver_version": "test-solver",
                "retrieval": {"mode": "skipped", "hits": [], "context": ""},
                "postprocess": {"applied": False, "policy": "unchanged"}}
    return solve


CONFIG = {"url": "http://127.0.0.1:1/v1", "model": "fixture", "temperature": 0.0,
          "max_tokens": 900, "timeout": 60, "enable_thinking": False, "strategy": "statutory_rag"}


def test_enhanced_run_records_scores_and_verifies(tmp_path):
    recorded: list[dict] = []
    with patch.object(enhanced, "solve", side_effect=_fake_solve(recorded)), \
            patch.object(enhanced, "corpus_fingerprint", return_value={"fixture": "hash"}), \
            patch.object(runner, "verify_model"):
        summary = enhanced.run(tasks=["1-2"], limit_per_task=2, sample_seed=42, config=CONFIG,
                               corpus_directories=["/frozen"], output_dir=tmp_path, run_dir=None,
                               resume=False, baseline=None)
    run_dir = next(path for path in tmp_path.iterdir() if path.is_dir())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (run_dir / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary["mean_score_all"] == 0.5
    assert manifest["pipeline_version"] == enhanced.PIPELINE_VERSION
    assert manifest["corpus_directories"] == ["/frozen"] and manifest["corpus_fingerprint"] == {"fixture": "hash"}
    assert len(manifest["samples"]) == 2 and len(rows) == 2
    assert sorted(row["score"] for row in rows) == [0.0, 1.0]
    assert all(row["scorer_version"] for row in rows)
    assert all(call["config"]["corpus_directories"] == ["/frozen"] for call in recorded)
    # The solver never receives the reference answer or the question id.
    assert all(set(call) == {"task", "instruction", "question", "config"} for call in recorded)
    assert all("正确答案" not in call["instruction"] for call in recorded)
    with patch.object(enhanced, "corpus_fingerprint", return_value={"fixture": "hash"}):
        verification = enhanced.verify_run(run_dir)
    assert verification["valid"] is True and verification["scoring_verified"] is True


def test_resume_replays_no_model_call_and_rejects_changed_manifest(tmp_path):
    recorded: list[dict] = []
    with patch.object(enhanced, "solve", side_effect=_fake_solve(recorded)), \
            patch.object(enhanced, "corpus_fingerprint", return_value={"fixture": "hash"}), \
            patch.object(runner, "verify_model"):
        enhanced.run(tasks=["1-2"], limit_per_task=2, sample_seed=42, config=CONFIG,
                     corpus_directories=["/frozen"], output_dir=tmp_path, run_dir=None,
                     resume=False, baseline=None)
    run_dir = next(path for path in tmp_path.iterdir() if path.is_dir())
    with patch.object(enhanced, "solve", side_effect=AssertionError("Resume must not call the model")), \
            patch.object(enhanced, "corpus_fingerprint", return_value={"fixture": "hash"}), \
            patch.object(runner, "verify_model"):
        summary = enhanced.run(tasks=["1-2"], limit_per_task=2, sample_seed=42, config=CONFIG,
                               corpus_directories=["/frozen"], output_dir=tmp_path, run_dir=run_dir,
                               resume=True, baseline=None)
    assert summary["total_questions"] == 2
    with patch.object(enhanced, "corpus_fingerprint", return_value={"fixture": "changed"}), \
            patch.object(runner, "verify_model"), \
            pytest.raises(ValueError, match="Resume manifest differs"):
        enhanced.run(tasks=["1-2"], limit_per_task=2, sample_seed=42, config=CONFIG,
                     corpus_directories=["/frozen"], output_dir=tmp_path, run_dir=run_dir,
                     resume=True, baseline=None)


def test_verify_detects_tampered_prediction(tmp_path):
    recorded: list[dict] = []
    with patch.object(enhanced, "solve", side_effect=_fake_solve(recorded)), \
            patch.object(enhanced, "corpus_fingerprint", return_value={"fixture": "hash"}), \
            patch.object(runner, "verify_model"):
        enhanced.run(tasks=["1-2"], limit_per_task=2, sample_seed=42, config=CONFIG,
                     corpus_directories=["/frozen"], output_dir=tmp_path, run_dir=None,
                     resume=False, baseline=None)
    run_dir = next(path for path in tmp_path.iterdir() if path.is_dir())
    detail = run_dir / "detailed_results.jsonl"
    rows = [json.loads(line) for line in detail.read_text(encoding="utf-8").splitlines()]
    rows[0]["prediction"] = "[正确答案]B<eoa>"
    detail.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    with patch.object(enhanced, "corpus_fingerprint", return_value={"fixture": "hash"}):
        verification = enhanced.verify_run(run_dir, write=False)
    assert verification["valid"] is False
    assert any(issue.startswith(("Checkpoint mismatch", "Score mismatch")) for issue in verification["issues"])
