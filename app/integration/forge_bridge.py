"""Bounded, per-process authenticated stdio bridge for the M2 offline pilot."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import selectors
import subprocess
import time
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.integration.forge_readonly import ReadonlyCaseSession
from app.llm.forge_governance import OfflineModelTest
from app.paths import ROOT

MAX_MESSAGE_BYTES = 2_000_000
WIRE_VERSION = 1


class SidecarProtocolError(RuntimeError):
    pass


def framework_binding(root: Path = ROOT) -> tuple[dict[str, Any], Path]:
    binding_path = root / "vendor/FRAMEWORK_BINDING.json"
    if binding_path.is_symlink():
        raise ValueError("framework binding cannot be a symbolic link")
    binding = json.loads(binding_path.read_text())
    version = binding["version_label"]
    # This pilot binds an exact reviewed artifact, not a client-selected path.
    if version != "0.8.3" or binding.get("sdk_api") != 1:
        raise ValueError("unsupported framework binding; requalify the bridge before upgrading")
    wheel = root / f"vendor/forgeharness-{version}-py3-none-any.whl"
    if wheel.is_symlink() or not wheel.resolve().is_relative_to(root.resolve()):
        raise ValueError("framework wheel must be an ordinary file inside the release")
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != binding["wheel_sha256"]:
        raise ValueError("framework wheel digest mismatch")
    return binding, wheel


def _read_message(process: subprocess.Popen[bytes], selector: selectors.BaseSelector,
                  pending: bytearray, deadline: float) -> dict[str, Any]:
    while b"\n" not in pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not selector.select(remaining):
            raise TimeoutError("framework sidecar deadline exceeded")
        assert process.stdout is not None
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            raise SidecarProtocolError("framework process ended before a complete response")
        pending.extend(chunk)
        if len(pending) > MAX_MESSAGE_BYTES:
            raise SidecarProtocolError("framework message exceeds the limit")
    line, _, rest = pending.partition(b"\n")
    pending[:] = rest
    value = json.loads(line)
    if not isinstance(value, dict):
        raise SidecarProtocolError("framework message must be an object")
    return value


def _send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(raw) > MAX_MESSAGE_BYTES:
        raise SidecarProtocolError("host response exceeds the limit")
    assert process.stdin is not None
    process.stdin.write(raw)
    process.stdin.flush()


def run_framework(session: ReadonlyCaseSession, python: Path, *, timeout_seconds: float = 120,
                  compare_native: bool = True,
                  model_test: OfflineModelTest | None = None) -> dict[str, Any]:
    """Run one fresh process; no source DB path or credentials cross the pipe.

    The interpreter and worker are trusted deployment configuration. This is
    process/environment separation, not an OS sandbox for untrusted Python.
    Calls are read-only, bounded, strictly sequenced and never automatically
    retried. Admission checks bracket each synchronous host computation.
    """
    if not 0 < timeout_seconds <= 600:
        raise ValueError("timeout_seconds must be in (0, 600]")
    if model_test is not None:
        model_test.validate()
    session.assert_current()
    binding, wheel = framework_binding()
    with zipfile.ZipFile(wheel) as archive:
        package_files = {name: hashlib.sha256(archive.read(name)).hexdigest()
                         for name in archive.namelist()
                         if name.startswith("forgeharness/") and name.endswith(".py")}
    token = secrets.token_hex(32)
    deadline = time.monotonic() + timeout_seconds
    session.backends.set_deadline(deadline)
    python = python.absolute()
    if not python.is_file():
        raise ValueError("configure the separate framework interpreter with setup_framework.py")
    with (session.directory / "sidecar-stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [str(python), "-I", str(ROOT / "app/integration/forge_worker.py")], cwd=session.directory,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"},
        )
        try:
            _send(process, {"wire_version": WIRE_VERSION, "token": token,
                            "version": binding["version_label"], "package_files": package_files,
                            "run_id": "pilot-" + secrets.token_hex(12),
                            "actor_id": hashlib.sha256(session.actor.name.encode()).hexdigest(),
                            "case_id": session.request.scope.case_id,
                            "corpus_version": session.corpus_identity,
                            "model_test": asdict(model_test) if model_test is not None else None,
                            "question": session.request.question.strip(),
                            "timeout_seconds": timeout_seconds})
            sequence = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                pending = bytearray()
                while True:
                    message = _read_message(process, selector, pending, deadline)
                    if (message.get("wire_version") != WIRE_VERSION
                            or not secrets.compare_digest(str(message.get("token", "")), token)):
                        raise SidecarProtocolError("framework authentication or protocol mismatch")
                    if message.get("type") == "result":
                        if (message.get("version") != binding["version_label"]
                                or message.get("sdk_api") != binding["sdk_api"]
                                or message.get("artifact_verified") is not True
                                or message.get("trace_valid") is not True):
                            raise SidecarProtocolError("framework artifact or SDK mismatch")
                        session.assert_current()
                        if message.get("status") == "succeeded" and "finish" not in session.outputs:
                            raise SidecarProtocolError("framework reported success before completing host nodes")
                        break
                    if message.get("type") != "node" or message.get("seq") != sequence + 1:
                        raise SidecarProtocolError("unexpected or repeated framework request")
                    sequence += 1
                    before_backends = len(session.backends.events)
                    output = session.call(message.get("node"))
                    if time.monotonic() > deadline:
                        raise TimeoutError("deadline exceeded during a read-only host node")
                    _send(process, {"wire_version": WIRE_VERSION, "token": token,
                                    "seq": sequence, "ok": True, "output": output,
                                    "backend_calls": session.backends.events[before_backends:]})
            assert process.stdin is not None
            process.stdin.close()
            if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
                raise SidecarProtocolError("framework process did not exit successfully")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                if pipe is not None and not pipe.closed:
                    pipe.close()
    return {"runtime": "forgeharness-readonly", "request_id": session.request.request_id,
            "framework_version": binding["version_label"],
            "wheel_sha256": binding["wheel_sha256"], "status": message["status"],
            "error": message.get("error"), "tool_calls": message["tool_calls"],
            "source_identity": session.source_identity, "access_log": session.access_log,
            "framework_trace": message["trace"], "framework_tool_evidence": message["evidence"],
            "framework_messages": message["messages"],
            "framework_usage": message.get("usage"),
            "offline_model_test": model_test is not None,
            "model_backend": "scripted-test" if model_test is not None else "disabled",
            "backend_calls": list(session.backends.events),
            "result": {k: v for k, v in session.outputs.get("finish", {}).items() if k != "bundle"},
            "comparison": session.compare_native() if compare_native and "finish" in session.outputs else None,
            "model_quality_assessed": False, "business_qualified": False,
            "source_business_writes": 0, "resumable": False}
