import json
import io
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest

from app.lawbench import TASK_NAMES, load_task
from scripts import benchmark_campaign as campaign


@pytest.fixture(autouse=True)
def no_service_probe():
    with patch.object(campaign, "probe_model"):
        yield


def make_campaign(directory: Path) -> Path:
    baseline = directory / "baseline"
    baseline.mkdir()
    rows = []
    for task in TASK_NAMES:
        for index, item in enumerate(load_task(task)[:50]):
            rows.append({"task": task, "question_id": f"{task}_{index:04d}", "question": item["question"],
                         "instruction": item["instruction"], "reference": item["answer"], "score": .5})
    campaign.write_rows(baseline / "detailed_results.jsonl", rows)
    output = directory / "campaign"
    campaign.prepare(baseline, output)
    return output


def fake_solve(task, instruction, question, config):
    assert "reference" not in config and "old_prediction" not in config
    assert instruction and question
    return {"prediction": "A", "error": None, "latency_ms": 1, "finish_reason": "stop",
            "usage": {"total_tokens": 1}, "calls": [{"usage": {"total_tokens": 1}}]}


def test_campaign_frozen_nonoverlapping_splits_and_no_gold_input(tmp_path):
    directory = make_campaign(tmp_path)
    rows = campaign.read_rows(directory / "inputs.jsonl")
    assert len({r["question_id"] for r in rows}) == 1600
    assert Counter(r["split"] for r in rows) == {"anchor": 1000, "development": 400, "confirmation": 200}
    assert all("reference" not in r and "answer" not in r for r in rows)
    assert all(Counter(r["task"] for r in rows if r["split"] == split) == {task: size for task in TASK_NAMES}
               for split, size in (("anchor", 50), ("development", 20), ("confirmation", 10)))
    campaign.validate_campaign(directory)
    with pytest.raises(FileExistsError):
        campaign.prepare(tmp_path / "baseline", directory)
    with (directory / "references.jsonl").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="Changed campaign file"):
        campaign.validate_campaign(directory)


def test_campaign_resume_preserves_completed_calls_and_rejects_changes(tmp_path):
    directory = make_campaign(tmp_path)
    with patch("app.benchmark_solver.solve", side_effect=[fake_solve("1-1", "i", "q", {}), KeyboardInterrupt()]):
        with pytest.raises(KeyboardInterrupt):
            campaign.run(directory, "test", "qwythos-direct", "development", 1)
    with patch("app.benchmark_solver.solve", side_effect=fake_solve) as solve:
        result = campaign.run(directory, "test", "qwythos-direct", "development", 1, True)
        assert solve.call_count == 19
    assert result["total"] == 20 and result["status"] == "completed"
    assert result["anchor_threshold_reached"] is False and result["goal_complete"] is False
    with pytest.raises(ValueError, match="Resume requires"):
        campaign.run(directory, "test", "qwythos-guided", "development", 1, True)
    with pytest.raises(ValueError, match="Invalid run name"):
        campaign.run(directory, "../escape", "qwythos-direct", "development", 1)


def test_campaign_does_not_zero_nonempty_truncation_or_skip_technical_errors(tmp_path):
    directory = make_campaign(tmp_path)

    def truncated(*args):
        return {**fake_solve(*args), "error": "ValueError: truncated_response: budget", "finish_reason": "length"}

    with patch("app.benchmark_solver.solve", side_effect=truncated):
        result = campaign.run(directory, "truncated", "qwythos-direct", "development", 1)
    assert result["total"] == result["truncated"] == 20
    rows = campaign.read_rows(directory / "runs/truncated/detailed_results.jsonl")
    assert all(row["output_warning"] for row in rows)
    assert all(row["score"] == 1 for row in rows if row["task"] == "3-6")
    with patch("app.benchmark_solver.solve", return_value={**fake_solve("x", "i", "q", {}), "prediction": "", "error": "ValueError: inference failed"}):
        failed = campaign.run(directory, "failed", "qwythos-direct", "development", 1)
    assert failed["total"] == failed["failed"] == 20 and failed["score_100"] == 0


@pytest.mark.parametrize("error", ["TimeoutError: deadline", "HTTPError: HTTP Error 409: Conflict", "IncompleteRead: zero bytes"])
def test_campaign_aborts_after_local_server_busy_signal(tmp_path, error):
    directory = make_campaign(tmp_path)
    failed = {**fake_solve("x", "i", "q", {}), "prediction": "", "error": error}
    with patch("app.benchmark_solver.solve", return_value=failed) as solve:
        with pytest.raises(RuntimeError, match="still occupy"):
            campaign.run(directory, "busy", "qwythos-direct", "development", 1)
    assert solve.call_count == 1
    summary = json.loads((directory / "runs/busy/summary.json").read_text())
    assert summary["status"] == "interrupted" and summary["total"] == summary["failed"] == 1


def test_verifier_recomputes_requests_and_detects_changed_final_answer(tmp_path):
    from app import benchmark_solver
    from scripts.verify_campaign import verify

    directory = make_campaign(tmp_path)

    def response(*args, **kwargs):
        return io.BytesIO(json.dumps({"model": "Qwythos-9B-v2-4bit-mlx",
                                     "choices": [{"message": {"content": "A"}, "finish_reason": "stop"}],
                                     "usage": {"completion_tokens": 1}}).encode())

    with patch.object(benchmark_solver.urllib.request, "build_opener") as opener:
        opener.return_value.open.side_effect = response
        campaign.run(directory, "verify", "qwythos-direct", "development", 1)
    output = directory / "runs/verify"
    assert verify(directory, output, False)["valid"] is True
    rows = campaign.read_rows(output / "detailed_results.jsonl")
    rows[0]["prediction"] = "ALTERED"
    campaign.write_rows(output / "detailed_results.jsonl", rows)
    checked = verify(directory, output, False)
    assert not checked["valid"]
    assert any("Final output" in issue for issue in checked["issues"])
