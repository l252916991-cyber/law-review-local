import io
import json
from unittest.mock import patch

import pytest

from scripts import benchmark_experiment as experiment
from scripts.verify_campaign import verify
from tests.test_benchmark_campaign import make_campaign


def test_external_profile_does_not_modify_campaign_and_resume_is_frozen(tmp_path):
    directory = make_campaign(tmp_path)
    original = (directory / "campaign.json").read_bytes()
    profile = tmp_path / "external.json"
    profile.write_text(json.dumps({"model": "fixture", "strategy": "task_guided"}))
    with patch.object(experiment, "probe_model"), patch("app.benchmark_solver.urllib.request.build_opener") as opener:
        opener.return_value.open.side_effect = lambda *a, **k: io.BytesIO(json.dumps({"choices": [{"message": {"content": "A"}, "finish_reason": "stop"}]}).encode())
        result = experiment.run(directory, "external", profile, per_task=1)
        assert result["total"] == 20
        experiment.run(directory, "external", profile, per_task=1, resume=True)
        assert opener.return_value.open.call_count == 20
        assert verify(directory, directory / "runs/external", False)["valid"]
        profile.write_text(json.dumps({"model": "changed", "strategy": "task_guided"}))
        with pytest.raises(ValueError, match="Resume requires"):
            experiment.run(directory, "external", profile, per_task=1, resume=True)
    assert (directory / "campaign.json").read_bytes() == original


def test_external_profile_can_run_one_selected_task(tmp_path):
    directory = make_campaign(tmp_path)
    profile = tmp_path / "single-task.json"
    profile.write_text(json.dumps({"model": "fixture", "strategy": "task_guided"}))
    with patch.object(experiment, "probe_model"), patch("app.benchmark_solver.urllib.request.build_opener") as opener:
        opener.return_value.open.side_effect = lambda *a, **k: io.BytesIO(json.dumps({
            "choices": [{"message": {"content": "A"}, "finish_reason": "stop"}]
        }).encode())
        result = experiment.run(directory, "single-task", profile, per_task=1, task_ids=("1-1",))
    assert result["total"] == 1
    manifest = json.loads((directory / "runs/single-task/manifest.json").read_text())
    assert manifest["selected_tasks"] == ["1-1"]
    assert verify(directory, directory / "runs/single-task", False)["valid"]


def test_external_profile_rejects_invalid_task_filters(tmp_path):
    directory = make_campaign(tmp_path)
    profile = tmp_path / "fixture.json"
    profile.write_text(json.dumps({"model": "fixture", "strategy": "task_guided"}))
    with pytest.raises(ValueError, match="task filter"):
        experiment.run(directory, "none", profile, task_ids=())
    with pytest.raises(ValueError, match="task filter"):
        experiment.run(directory, "duplicate", profile, task_ids=("1-1", "1-1"))


def test_external_profile_rejects_gold_and_remote_endpoint(tmp_path):
    profile = tmp_path / "bad.json"
    profile.write_text(json.dumps({"reference": "gold"}))
    with pytest.raises(ValueError, match="Unexpected"):
        experiment.configuration(profile)
    profile.write_text(json.dumps({"url": "https://example.com/v1"}))
    with pytest.raises(ValueError, match="loopback"):
        experiment.configuration(profile)


def test_status_does_not_reuse_stale_verification(tmp_path):
    from scripts.score85_status import summarize
    directory = make_campaign(tmp_path)
    profile = tmp_path / "fixture.json"
    profile.write_text(json.dumps({"model": "fixture", "strategy": "task_guided"}))
    with patch.object(experiment, "probe_model"), patch("app.benchmark_solver.urllib.request.build_opener") as opener:
        opener.return_value.open.side_effect = lambda *a, **k: io.BytesIO(json.dumps({"choices": [{"message": {"content": "A"}, "finish_reason": "stop"}]}).encode())
        experiment.run(directory, "status", profile, per_task=1)
    output = directory / "runs/status"
    verify(directory, output)
    assert summarize(directory)["attempts"][0]["verified"]
    with (output / "detailed_results.jsonl").open("a") as stream:
        stream.write("\n")
    assert not summarize(directory)["attempts"][0]["verified"]
