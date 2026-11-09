"""Demo identity report: binds commit, code, datasets and build digest without a model."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.ops.demo_identity_report import build_report, frontend_binding, git_state, main

ROOT = Path(__file__).resolve().parents[1]


def test_report_binds_identity_without_a_model_or_case_content():
    with patch("app.evaluation.collaboration_ablation.precheck",
               return_value={"endpoint": "http://127.0.0.1:8000/v1", "model": "synthetic-model",
                             "available": False, "max_tokens": 900, "temperature": 0.2,
                             "seed_sent": False, "checked_at": "2026-10-27T00:00:00+0800"}):
        report = build_report(ROOT)
    # A source archive is not a git checkout: the report must say so instead of failing.
    assert report["git"]["commit"] is None or len(report["git"]["commit"]) == 40
    assert report["model"]["available"] is False
    assert {"prompt_version", "policy_version", "judge_prompt_version"} <= set(report["identity"])
    assert report["datasets"]["dev"]["items"] == 60
    assert report["datasets"]["holdout"]["items"] == 24
    assert len(report["backend_files"]) >= 6
    assert all(len(value) == 64 for value in report["backend_files"].values())
    assert "app/main.py" in report["loaded_backend"]["module_file"] or "app" in report["loaded_backend"]["module_file"]
    serialized = json.dumps(report, ensure_ascii=False)
    assert "起诉意见书" not in serialized  # identity only, never case text
    assert report["limits"]


def test_frontend_binding_is_reported_missing_when_not_built(tmp_path):
    binding = frontend_binding(tmp_path)
    assert binding["present"] is False and "未构建" in binding["note"]
    (tmp_path / "frontend/out").mkdir(parents=True)
    (tmp_path / "frontend/out/build-info.json").write_text(
        json.dumps({"format_version": 1, "assets": [{"path": "index.html", "sha256": "0" * 64}]}),
        encoding="utf-8")
    built = frontend_binding(tmp_path)
    assert built["present"] is True and built["assets"] == 1 and len(built["sha256"]) == 64


def test_git_state_is_null_outside_a_repository(tmp_path):
    state = git_state(tmp_path)
    assert state == {"commit": None, "branch": None, "dirty": None, "dirty_paths": []}


def test_cli_writes_once_and_refuses_overwrite(tmp_path):
    output = tmp_path / "identity.json"
    with patch("app.evaluation.collaboration_ablation.precheck",
               return_value={"available": False, "model": "synthetic-model", "endpoint": "http://127.0.0.1:1/v1"}):
        assert main(["--output", str(output), "--root", str(ROOT)]) == 0
        assert main(["--output", str(output), "--root", str(ROOT)]) != 0
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 1


def test_report_is_valid_json_written_exclusively(tmp_path):
    output = tmp_path / "identity.json"
    with patch("app.evaluation.collaboration_ablation.precheck",
               return_value={"available": False, "model": "synthetic-model", "endpoint": "http://127.0.0.1:1/v1"}):
        main(["--output", str(output), "--root", str(ROOT)])
    assert output.read_text(encoding="utf-8").endswith("}\n")


def test_report_does_not_call_git_with_untrusted_arguments(tmp_path):
    # git_state only runs fixed read-only commands; a directory without git must not raise.
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    assert git_state(tmp_path)["commit"] is None
    with pytest.raises(SystemExit):
        main([])
