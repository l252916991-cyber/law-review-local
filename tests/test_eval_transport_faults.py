"""Transport faults use actual loopback TCP in isolated child processes.

The child is explicitly a controlled-fault endpoint drill. It cannot inherit
application data/model settings, and does not use paid or production services.
The suite's parent process can retain pytest-socket's general network ban.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error

import pytest

from scripts.eval_transport_faults import ROOT, failure_class, local_endpoint, main, run


def test_cli_requires_opt_in_before_creating_any_output(tmp_path):
    output = tmp_path / "not-created"
    with pytest.raises(SystemExit) as error:
        main(["--output", str(output)])
    assert error.value.code == 2
    assert not output.exists()
    with pytest.raises(ValueError, match="Explicit"):
        run(output, allow_loopback_fault_server=False)
    assert not output.exists()


def test_protocol_refuses_small_sample_or_reusing_existing_output(tmp_path):
    output = tmp_path / "small"
    with pytest.raises(ValueError, match="At least five"):
        run(output, allow_loopback_fault_server=True, repeats=4)
    assert not output.exists()
    marker = tmp_path / "preserved.txt"
    marker.write_text("existing synthetic marker")
    with pytest.raises(ValueError, match="new directory"):
        run(tmp_path, allow_loopback_fault_server=True)
    assert marker.read_text() == "existing synthetic marker"


def test_endpoint_rejects_remote_destinations_credentials_and_path_changes():
    assert local_endpoint("http://127.0.0.1:12345/v1") == "http://127.0.0.1:12345/v1"
    for value in ("http://example.com/v1", "http://127.0.0.1.example.com:123/v1",
                  "http://127.0.0.1/v1", "http://user:secret@127.0.0.1:123/v1",
                  "http://127.0.0.1:123/v1?secret=x", "http://127.0.0.1:123/other",
                  "https://127.0.0.1:123/v1"):
        with pytest.raises(ValueError):
            local_endpoint(value)


def test_error_classification_walks_wrappers_without_exposing_messages():
    outer = RuntimeError("private provider response must not be copied")
    outer.__cause__ = urllib.error.HTTPError("http://127.0.0.1/", 503, "private", {}, None)
    category, names = failure_class(outer)
    assert category == "interface_5xx"
    assert names == ["RuntimeError", "HTTPError"]
    assert "private" not in json.dumps({"category": category, "types": names})
    outer.__cause__ = TimeoutError("private")
    assert failure_class(outer)[0] == "return_timeout"
    outer.__cause__ = outer  # malformed exception cycles still terminate
    assert failure_class(outer) == ("unexpected_client_error", ["RuntimeError"])


@pytest.fixture(scope="module")
def actual_transport_samples():
    # Explicit subprocess scope keeps the parent socket ban intact while still
    # proving real HTTP behaviour. No pytest plugin is loaded in the child.
    code = """
import json
from scripts.eval_transport_faults import FAULTS, PATHS, run_attempt
rows = [run_attempt(mode, path) for mode in ('healthy', *FAULTS) for path in PATHS]
print(json.dumps(rows))
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAW_REVIEW_")}
    process = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=90, check=False)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout.strip().splitlines()[-1])


def test_positive_controls_prove_generation_and_persistence_are_exercised(actual_transport_samples):
    controls = [row for row in actual_transport_samples if row["mode"] == "healthy"]
    assert len(controls) == 2
    assert all(row["passed"] and row["llm_used"] and row["raw_answer_received"] for row in controls)
    assert all(row["injection_reached_generation"] for row in controls)
    assert next(row for row in controls if row["client_path"] == "chat_persistence")["assistant_messages"] == 1
    assert next(row for row in controls if row["client_path"] == "ablation_generation")["assistant_messages"] == 0


def test_all_four_real_faults_fail_closed_on_both_client_paths(actual_transport_samples):
    failures = [row for row in actual_transport_samples if row["mode"] != "healthy"]
    assert len(failures) == 8
    assert all(row["passed"] and row["injection_reached_generation"] for row in failures)
    assert all(row["failure_class"] == row["mode"] for row in failures)
    assert not any(row["success_reported"] or row["raw_answer_received"] for row in failures)
    assert not any(row["successful_answer_persisted"] for row in failures)
    assert all(row["integrity_ok"] for row in failures)


def test_return_phase_faults_really_send_headers_and_answer_prefix(actual_transport_samples):
    partials = [row for row in actual_transport_samples if row["mode"] in {"return_timeout", "truncated_return"}]
    assert len(partials) == 4
    for row in partials:
        assert any(event.get("http_status") == 200 for event in row["endpoint_events"])
        sent = next(event for event in row["endpoint_events"] if event["stage"] == "partial_body_sent")
        assert 0 < sent["bytes"] < sent["declared_bytes"]
        assert sent["contains_scripted_answer"]
        assert not row["partial_output_misclassified_as_success"]
        assert not row["task_process_killed"]


def test_task_deadline_really_kills_waiting_process_without_saving_answer(actual_transport_samples):
    deadlines = [row for row in actual_transport_samples if row["mode"] == "task_deadline"]
    assert len(deadlines) == 2
    assert all(row["task_process_killed"] for row in deadlines)
    assert all("EvaluatorProcessDeadline" in row["exception_types"] for row in deadlines)
    assert not any(event["stage"] == "headers_sent" for row in deadlines for event in row["endpoint_events"])
    assert all(row["assistant_messages"] == 0 for row in deadlines)
