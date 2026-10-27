"""Recovery demo: reuse a saved review, refuse a stale source, never re-run experts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.demo_recovery_check import main


def _evidence(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_interrupt_then_resume_reuses_the_saved_review_without_new_steps(tmp_path):
    data = tmp_path / "case"
    first = tmp_path / "interrupt.json"
    assert main(["--phase", "interrupt", "--data-dir", str(data), "--output", str(first)]) == 0
    saved = _evidence(first)
    assert saved["run"]["status"] == "failed" and saved["steps"] > 0

    second = tmp_path / "resume.json"
    assert main(["--phase", "resume", "--data-dir", str(data), "--output", str(second),
                 "--state", str(first)]) == 0
    resumed = _evidence(second)
    assert resumed["collaboration_reused"] is True
    assert resumed["steps_after"] == saved["steps"]
    assert resumed["resume_count"] == 1 and resumed["run"]["status"] == "completed"
    assert resumed["model_disabled"] is True


def test_tampered_page_refuses_resume_without_consuming_a_retry(tmp_path):
    data = tmp_path / "case"
    output = tmp_path / "tamper.json"
    assert main(["--phase", "tamper", "--data-dir", str(data), "--output", str(output)]) == 0
    record = _evidence(output)
    assert record["outcome"] == "refused"
    assert "内容已变化" in record["detail"]
    assert record["run"]["resume_count"] == 0 and record["run"]["status"] == "failed"


def test_resume_requires_state_and_an_existing_database(tmp_path):
    with pytest.raises(SystemExit):
        main(["--phase", "resume", "--data-dir", str(tmp_path / "missing"),
              "--output", str(tmp_path / "x.json"), "--state", str(tmp_path / "s.json")])


def test_interrupt_refuses_an_existing_data_directory(tmp_path):
    data = tmp_path / "case"
    data.mkdir()
    assert main(["--phase", "interrupt", "--data-dir", str(data),
                 "--output", str(tmp_path / "x.json")]) != 0
    assert not (tmp_path / "x.json").exists()
