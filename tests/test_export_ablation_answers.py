"""Answer export retains paid failures and distinguishes unknown usage from zero."""
import hashlib
import json

import pytest

from scripts.export_ablation_answers import export


def batch(tmp_path, rows):
    (tmp_path / "items").mkdir()
    (tmp_path / "run").mkdir()
    ids = sorted({row["item"] for row in rows})
    (tmp_path / "manifest.json").write_text(json.dumps({"items": [{"id": item} for item in ids],
                                                       "dataset": {"name": "synthetic"}}))
    for item in ids:
        (tmp_path / "items" / f"{item}.json").write_text(json.dumps({"contexts": []}))
    (tmp_path / "run/attempts.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return tmp_path


def test_export_preserves_original_usage_time_and_failed_outcome(tmp_path):
    root = batch(tmp_path, [
        {"item": "q1", "group": "A", "repeat": 0, "raw_answer": "原始不合规回答", "outcome": "contract_failed",
         "executed": True, "passed": False, "input_tokens": 103, "output_tokens": 17,
         "latency_ms": 1200, "elapsed_seconds": 1.3, "usage_known": True},
        {"item": "q2", "group": "A", "repeat": 0, "outcome": "not_attempted", "executed": False,
         "passed": False, "usage_known": False},
    ])
    result = export(root, "A")
    one, two = result["answers"]["q1"], result["answers"]["q2"]
    assert one["outcome"] == "contract_failed" and not one["passed"]
    assert (one["input_tokens"], one["output_tokens"], one["latency_ms"], one["elapsed_seconds"]) == (103, 17, 1200, 1.3)
    assert two["outcome"] == "not_attempted" and two["executed"] is False
    assert two["input_tokens"] is two["output_tokens"] is two["latency_ms"] is two["elapsed_seconds"] is None
    assert result["attempts_sha256"] == hashlib.sha256((root / "run/attempts.jsonl").read_bytes()).hexdigest()


def test_export_rejects_duplicate_attempts_instead_of_hiding_them(tmp_path):
    row = {"item": "q1", "group": "A", "repeat": 0}
    root = batch(tmp_path, [row, row])
    with pytest.raises(ValueError, match="重复"):
        export(root, "A")


def test_export_does_not_replace_missing_repeat_with_another_sample(tmp_path):
    root = batch(tmp_path, [{"item": "q1", "group": "A", "repeat": 1}])
    with pytest.raises(ValueError, match="没有记录"):
        export(root, "A", repeat=0)
