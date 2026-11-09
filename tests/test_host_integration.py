from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app.integration.forge_bridge import SidecarProtocolError
from app.integration.host_integration import IsolatedHostIntegration, worker_slot
from app.integration.host_writes import ISOLATED_RUNTIME
from app.integration.runtime_adapter import current_actor_identity
from app.integration.runtime_ports import (
    ContractValidationError,
    ModelConfig,
    RequestConflictError,
    RequestInProgressError,
    RunRequest,
    RunScope,
    UnsupportedCapabilityError,
)
from app.storage.db import connect, db_scope, init_db, transaction
from tests.test_host_writes import isolated  # noqa: F401

FRAMEWORK_PYTHON = os.getenv("LAW_REVIEW_FORGE_PYTHON", "")
actual = pytest.mark.skipif(not FRAMEWORK_PYTHON, reason="independent installed framework Python3.12 required")


def request(**changes):
    return replace(RunRequest(request_id="process-request", scope=RunScope(1), actor=current_actor_identity(),
                      question="合成闭环请求", runtime=ISOLATED_RUNTIME,
                      model=ModelConfig(use_llm=False), use_remote_embeddings=False), **changes)


def process(path, mode, *, expect_crash=False):
    result = subprocess.run([sys.executable, "-m", "tests.host_integration_driver", str(path), mode],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ, "LAW_REVIEW_DATA_DIR": str(path.parent)},
        capture_output=True, text=True, timeout=30, start_new_session=True)
    if expect_crash:
        assert result.returncode == -signal.SIGKILL, result.stderr
        return None
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def counts():
    with closing(connect()) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            "runtime_requests", "host_runtime_links", "agent_runs", "conversations", "messages",
            "memories", "gap_detections", "agent_steps", "audit_log", "host_write_receipts")}


def assert_unique_result():
    observed = counts()
    assert observed == {"runtime_requests": 1, "host_runtime_links": 1, "agent_runs": 1,
        "conversations": 2, "messages": 2, "memories": 1, "gap_detections": 1,
        "agent_steps": 1, "audit_log": 1, "host_write_receipts": 7}


def test_durable_accept_and_queue_cancellation(isolated):
    control = IsolatedHostIntegration()
    first = control.accept(request())
    again = control.accept(request())
    assert again["duplicate"] and first["run_id"] == again["run_id"]
    with pytest.raises(RequestConflictError):
        control.accept(request(question="不同参数"))
    assert control.cancel(first["request_id"])["status"] == "cancelled"
    assert control.cancel(first["request_id"])["cancel_requested"]
    assert control.drive(first["request_id"])["status"] == "cancelled"
    assert counts()["messages"] == 0
    init_db(seed=False, recover_runs=True)
    assert control.status(first["request_id"])["status"] == "cancelled"


def test_concurrent_request_single_association(isolated):
    req = request()
    def accept():
        with db_scope(isolated):
            return IsolatedHostIntegration().accept(req)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: accept(), range(8)))
    assert len({r["run_id"] for r in results}) == 1
    assert sum(not r["duplicate"] for r in results) == 1


def test_persistent_admission_and_single_release(isolated):
    control = IsolatedHostIntegration()
    for i in range(4):
        control.accept(request(request_id=f"request-{i}"))
    with pytest.raises(RequestInProgressError):
        control.accept(request(request_id="overflow"))
    control.cancel("request-0")
    control.cancel("request-0")
    restarted = IsolatedHostIntegration()
    restarted.accept(request(request_id="replacement"))
    with pytest.raises(RequestInProgressError):
        restarted.accept(request(request_id="still-full"))


def test_two_worker_leases_and_same_request_lock(isolated):
    directory = isolated.parent / "leases"
    with worker_slot(directory, "one"):
        with pytest.raises(RequestInProgressError, match="active worker"):
            with worker_slot(directory, "one"):
                pass
        with worker_slot(directory, "two"):
            with pytest.raises(RequestInProgressError, match="two workers"):
                with worker_slot(directory, "three"):
                    pass
    with worker_slot(directory, "three"):
        pass


def test_event_reordering_dedup_and_terminal_monotonicity(isolated):
    control = IsolatedHostIntegration()
    control.accept(request())
    events = [{"host_event_version": 1, "revision": i, "event_id": f"event-{i}", "status": s} for i, s in enumerate(
              ["running", "running", "succeeded"], start=1)]
    result = {"revision": 3, "status": "succeeded", "usage": {"tool_calls": 6}}
    control._project("process-request", list(reversed(events)), result)
    control._project("process-request", events + events, result)
    assert control.status("process-request")["cursor"] == 3
    with pytest.raises(SidecarProtocolError, match="regress"):
        control._project("process-request", [{"host_event_version": 1, "revision": 4, "event_id": "four", "status": "running"}],
                         {"revision": 4, "status": "running"})
    with pytest.raises(SidecarProtocolError, match="identity conflict"):
        control._project("process-request", [{**events[0], "status": "failed"}], result)
    assert control.status("process-request")["status"] == "succeeded"


def test_missing_events_do_not_release_admission(isolated):
    control = IsolatedHostIntegration()
    control.accept(request())
    with pytest.raises(SidecarProtocolError, match="incomplete"):
        control._project("process-request", [{"host_event_version": 1, "revision": 2, "event_id": "two", "status": "succeeded"}],
                         {"revision": 2, "status": "succeeded"})
    assert control.status("process-request")["status"] == "queued"


@actual
def test_installed_wheel_new_host_process_roundtrip(isolated):
    first = process(isolated, "normal")
    assert first["status"] == "succeeded"
    assert first["result"]["usage"]["tool_calls"] == 6
    joined = next(n for n in first["result"]["nodes"] if n["id"] == "joined")
    assert joined["status"] == "completed" and joined["used"]["tool_calls"] == 0
    repeated = process(isolated, "normal")
    assert repeated["result"] == first["result"]
    assert_unique_result()
    rebuilt = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON).rebuild_projection("process-request")
    assert rebuilt["result"] == first["result"]
    assert_unique_result()


@actual
@pytest.mark.parametrize("boundary", ["accept", "response", "journal", "projection"])
def test_sigkill_persistent_boundary_recovery(isolated, boundary):
    process(isolated, boundary, expect_crash=True)
    before = counts()
    if boundary in {"response", "journal", "projection"}:
        assert before["memories"] == 1
    recovered = process(isolated, "normal")
    assert recovered["status"] == "succeeded"
    assert recovered["result"]["usage"]["tool_calls"] == 6
    assert_unique_result()


@actual
@pytest.mark.parametrize("boundary", ["before", "inside"])
def test_precommit_sigkill_rolls_back_and_stays_unknown(isolated, boundary):
    process(isolated, boundary, expect_crash=True)
    assert counts()["memories"] == 0
    # Framework journal says dispatched, host has no receipt. Do NOT assume
    # absence makes it safe to replay or erase the physical attempt reservation.
    recovered = process(isolated, "normal")
    assert recovered["status"] == "running" and recovered["needs_reconciliation"]
    assert counts()["memories"] == 0
    node = next(n for n in recovered["result"]["nodes"] if n["id"] == "draft")
    assert node["status"] == "unknown" and node["reservation"]["tool_calls"] == 1


@actual
def test_pending_approval_restart_and_repeat(isolated):
    first = process(isolated, "approval")
    assert first["status"] == "awaiting_approval"
    assert counts()["audit_log"] == 0
    recovered = process(isolated, "normal")
    assert recovered["status"] == "awaiting_approval"
    approved = process(isolated, "approve")
    assert approved["status"] == "succeeded"
    assert_unique_result()
    with pytest.raises(AssertionError):
        process(isolated, "approve")
    assert_unique_result()


@actual
def test_stale_source_blocks_history_and_recovery(isolated):
    process(isolated, "normal")
    with transaction() as conn:
        conn.execute("UPDATE cases SET description='资料变更' WHERE id=1")
    for operation in (lambda: IsolatedHostIntegration().status("process-request"),
                      lambda: IsolatedHostIntegration().drive("process-request")):
        with pytest.raises(RequestConflictError, match="source changed"):
            operation()


@actual
def test_cancel_running_stops_new_effects(isolated):
    result = process(isolated, "cancel")
    assert result["status"] == "cancelled"
    assert counts()["messages"] == 1
    assert counts()["memories"] == counts()["audit_log"] == 0
    assert process(isolated, "normal")["status"] == "cancelled"


@actual
def test_cancel_reconciles_committed_effect_without_new_dispatch(isolated):
    process(isolated, "response", expect_crash=True)
    control = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON)
    control.cancel("process-request")
    result = process(isolated, "normal")
    assert result["status"] == "cancelled"
    assert counts()["memories"] == 1
    assert counts()["messages"] == 1
    assert counts()["audit_log"] == 0


@actual
def test_original_absolute_deadline_never_renewed(isolated):
    control = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON, timeout_seconds=0.02)
    control.accept(request())
    time.sleep(0.04)
    result = control.drive("process-request")
    assert result["status"] == "exhausted"
    assert counts()["messages"] == 0
    assert result["result"] == {}  # no framework run or model call was admitted
    assert control.drive("process-request")["status"] == "exhausted"


@actual
@pytest.mark.parametrize("command", ["recover", "inspect", "rebuild"])
def test_verified_historical_success_remains_readable_after_deadline(isolated, monkeypatch, command):
    import app.integration.host_integration as integration

    control = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON)
    control.accept(request())
    original = control.drive("process-request")
    observed = counts()
    with closing(connect()) as conn:
        deadline = conn.execute("SELECT deadline FROM host_runtime_links").fetchone()[0]
    monkeypatch.setattr(integration.time, "time", lambda: deadline + 1)
    restarted = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON)
    result = (restarted.rebuild_projection("process-request") if command == "rebuild"
              else restarted.drive("process-request", command=command))
    assert result["status"] == "succeeded" and not result["needs_reconciliation"]
    assert result["result"] == original["result"]
    assert result["framework_run_id"] == original["framework_run_id"]
    assert counts() == observed
    with closing(connect()) as conn:
        assert conn.execute("SELECT deadline FROM host_runtime_links").fetchone()[0] == deadline


@actual
def test_different_historical_result_is_not_released_after_deadline(isolated, monkeypatch):
    import app.integration.host_integration as integration

    control = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON)
    control.accept(request())
    original = control.drive("process-request")
    changed = {**original["result"], "final_output": "changed host projection"}
    with transaction() as conn:
        deadline = conn.execute("SELECT deadline FROM host_runtime_links").fetchone()[0]
        conn.execute("UPDATE host_runtime_links SET result_json=?", (json.dumps(changed),))
    observed = counts()
    monkeypatch.setattr(integration.time, "time", lambda: deadline + 1)
    with pytest.raises(TimeoutError, match="late framework completion"):
        control.drive("process-request", command="inspect")
    assert counts() == observed


@actual
def test_first_success_received_after_deadline_still_requires_reconciliation(isolated, monkeypatch):
    import app.integration.host_integration as integration

    control = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON)
    control.accept(request())
    with closing(connect()) as conn:
        deadline = conn.execute("SELECT deadline FROM host_runtime_links").fetchone()[0]
    read_message = integration._read_message

    def delayed_result(*args, **kwargs):
        message = read_message(*args, **kwargs)
        if message.get("type") == "result":
            monkeypatch.setattr(integration.time, "time", lambda: deadline + 1)
        return message

    monkeypatch.setattr(integration, "_read_message", delayed_result)
    with pytest.raises(TimeoutError, match="late framework completion"):
        control.drive("process-request")
    result = control.status("process-request")
    assert result["status"] == "unknown" and result["needs_reconciliation"]
    assert result["result"] == {}
    with closing(connect()) as conn:
        assert conn.execute("SELECT deadline FROM host_runtime_links").fetchone()[0] == deadline


@actual
def test_delete_cascade_and_framework_state_erasure(isolated):
    result = process(isolated, "normal")
    directory = isolated.parent / "host-framework-state" / result["framework_run_id"]
    assert directory.is_dir()
    with transaction() as conn:
        conn.execute("DELETE FROM cases WHERE id=1")
    control = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON)
    assert control.purge_deleted_state() == 1
    assert control.purge_deleted_state() == 0
    assert not directory.exists()
    assert counts()["host_write_receipts"] == counts()["host_runtime_links"] == 0


@actual
def test_optional_draft_disabled(isolated):
    control = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON)
    control.accept(request(persist_memory=False))
    result = control.drive("process-request")
    assert result["status"] == "succeeded"
    assert counts()["memories"] == 0
    assert result["result"]["usage"]["tool_calls"] == 5


@actual
def test_missing_framework_state_never_restarts_authorized_dispatch(isolated):
    process(isolated, "response", expect_crash=True)
    control = IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON)
    handle = control.status("process-request")
    before = counts()
    shutil.rmtree(isolated.parent / "host-framework-state" / handle["framework_run_id"] / "state")
    with pytest.raises(SidecarProtocolError):
        control.drive("process-request")
    assert control.status("process-request")["status"] == "unknown"
    assert counts() == before


@actual
def test_approval_cannot_cross_changed_action(isolated):
    process(isolated, "approval")
    with closing(connect()) as conn:
        original = conn.execute("SELECT graph_json FROM host_runtime_links").fetchone()[0]
    actions = json.loads(original)
    actions[-1]["payload"]["detail"] = "changed action after approval wait"
    with transaction() as conn:
        conn.execute("UPDATE host_runtime_links SET graph_json=?", (json.dumps(actions),))
    with pytest.raises(SidecarProtocolError):
        IsolatedHostIntegration(framework_python=FRAMEWORK_PYTHON).drive("process-request", command="approve")
    assert counts()["audit_log"] == 0
    with transaction() as conn:
        conn.execute("UPDATE host_runtime_links SET graph_json=?", (original,))
    assert process(isolated, "approve")["status"] == "succeeded"


@actual
def test_version_mismatch_no_fallback(isolated):
    control = IsolatedHostIntegration()
    control.accept(request())
    with transaction() as conn:
        conn.execute("UPDATE host_runtime_links SET binding_json=json_set(binding_json,'$.protocol',999)")
    with pytest.raises(UnsupportedCapabilityError, match="no fallback"):
        control.drive("process-request")
    assert counts()["messages"] == 0


def test_unsupported_input_and_database_binding(isolated):
    control = IsolatedHostIntegration()
    with pytest.raises(UnsupportedCapabilityError):
        control.accept(request(model=ModelConfig(use_llm=True)))
    with pytest.raises(ContractValidationError):
        IsolatedHostIntegration(timeout_seconds=0)
    with pytest.raises(ContractValidationError):
        control.drive("missing", command="unknown")
