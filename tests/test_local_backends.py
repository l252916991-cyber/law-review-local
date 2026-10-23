from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app.db import connect, get_db_path, init_db, transaction
from app.host_writes import digest
from app.local_backends import BackendGate, BackendUnknown, LocalModels
from app.model_domain import LocalModelDomainIntegration
from app.runtime_ports import ContractValidationError, ModelConfig, RequestConflictError
from tests.test_domain_integration import counts, request
from tests.test_domain_writes import domain  # noqa: F401
from tests.test_host_writes import isolated  # noqa: F401

actual = pytest.mark.skipif(not os.getenv("LAW_REVIEW_FORGE_PYTHON"), reason="installed framework Python3.12 required")


def identities(directory):
    result = {}
    for kind in ("llm", "embedding", "reranker"):
        folder = directory / kind
        folder.mkdir()
        entries = {}
        for name, body in (("config.json", b'{"synthetic":true}'), ("model.safetensors", b"synthetic weight fixture")):
            path = folder / name
            path.write_bytes(body)
            entries[name] = {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "mtime_ns": path.stat().st_mtime_ns}
        result[kind] = {"path": str(folder), "files": entries, "identity": "local-sha256:" + digest(entries)}
    return result


@pytest.fixture
def local_models(domain, tmp_path):
    calls = []
    def transport(kind, payload, timeout):
        calls.append(kind)
        if kind == "llm":
            return {"model": "llm", "choices": [{"message": {"content": "合成审批记录 [资料1]。请律师复核原文与上述结论。"}}],
                    "usage": {"prompt_tokens": 24, "completion_tokens": 8}}
        if kind == "embedding":
            return {"model": "embedding", "data": [{"index": i, "embedding": [1.0,0.0]} for i in range(len(payload["input"]))],
                    "usage": {"prompt_tokens": 4}}
        return {"model": "reranker", "results": [{"index": i, "relevance_score": 1/(i+1)}
                for i in range(len(payload["documents"]))], "usage": {"total_tokens": 16}}
    models = LocalModels(endpoint="http://127.0.0.1:1/v1", identities=identities(tmp_path),
                         models={k:k for k in ("llm", "embedding", "reranker")}, transport=transport)
    return models, calls


def gate_for(models):
    control = LocalModelDomainIntegration(models=models)
    control.accept(request(model=ModelConfig(use_llm=True), use_remote_embeddings=True))
    with closing(connect()) as conn:
        row = control._load(conn, "domain-process")
    return control, BackendGate(control, row, models)


def llm_payload(models):
    return {"model": "llm", "messages": [{"role":"user", "content":"合成测试"}],
            "temperature": models.temperature, "max_tokens": models.max_tokens, "stream": False}


@pytest.mark.parametrize("endpoint", ["http://example.invalid/v1","https://127.0.0.1/v1",
    "http://localhost/v1","http://user:password@127.0.0.1/v1","http://127.0.0.1/v1?redirect=x"])
def test_local_registry_rejects_egress_and_unbound_routes(tmp_path, endpoint):
    with pytest.raises(ContractValidationError):
        LocalModels(endpoint=endpoint, identities={}, models={})


def test_actual_file_mutation_and_config_mutation_reject(local_models):
    models, _ = local_models
    path = Path(models.identities["llm"]["path"]) / "model.safetensors"
    path.write_bytes(b"changed")
    with pytest.raises(RequestConflictError, match="artifacts"):
        models.assert_current()


def test_known_response_reuses_provider_and_conflicts_are_rejected(local_models):
    models, calls = local_models
    _, gate = gate_for(models)
    payload = llm_payload(models)
    first = gate.call("llm", payload, "direct-call")
    again = gate.call("llm", payload, "direct-call")
    assert first == again and first["input_tokens"] == 24 and first["output_tokens"] == 8
    assert calls == ["llm"]
    payload["messages"][0]["content"] = "changed"
    with pytest.raises(RequestConflictError, match="input changed"):
        gate.call("llm", payload, "direct-call")
    with transaction() as conn:
        conn.execute("UPDATE host_backend_calls SET response_json='{}'")
    with pytest.raises(RequestConflictError, match="response changed"):
        gate.lookup("direct-call")


def test_unknown_dispatch_and_missing_usage_are_never_retried(local_models):
    models, calls = local_models
    _, gate = gate_for(models)
    original = models.transport
    def fail(*args):
        calls.append("failed")
        raise OSError("connection lost")
    models.transport = fail
    with pytest.raises(BackendUnknown):
        gate.call("llm", llm_payload(models), "unknown-call")
    with pytest.raises(BackendUnknown):
        gate.call("llm", llm_payload(models), "unknown-call")
    assert calls == ["failed"]
    models.transport = lambda *args: {"choices": [{"message": {"content": "unmeasured"}}]}
    record = gate.call("llm", llm_payload(models), "usage-unknown")
    assert record["status"] == "response_known_usage_unknown" and record["input_tokens"] is None
    assert gate.lookup("usage-unknown")["response_json"] is not None
    models.transport = original


@actual
def test_sdk_model_and_retrieval_measurements_persist_and_recover(local_models):
    models, calls = local_models
    control, _ = gate_for(models)
    result = control.drive("domain-process")
    assert result["status"] == "succeeded", json.dumps(result["result"], indent=2)
    assert "llm" in calls and "embedding" in calls and "reranker" in calls
    with closing(connect()) as conn:
        incoming, outgoing, provider_calls = conn.execute(
            "SELECT SUM(input_tokens),SUM(output_tokens),COUNT(*) FROM host_backend_calls").fetchone()
        assert all(r[0] == "completed" for r in conn.execute("SELECT status FROM host_backend_calls"))
    assert result["result"]["usage"]["input_tokens"] == incoming
    assert result["result"]["usage"]["output_tokens"] == outgoing == 8
    assert result["result"]["usage"]["steps"] == 1
    observed, before = counts(), list(calls)
    assert control.drive("domain-process")["result"] == result["result"]
    assert calls == before and counts() == observed
    assert provider_calls == len(calls)


@actual
def test_unknown_model_usage_cannot_be_zero_or_completed(local_models):
    models, calls = local_models
    original = models.transport
    def without_usage(kind, payload, timeout):
        result = original(kind, payload, timeout)
        if kind == "llm":
            result.pop("usage")
        return result
    models.transport = without_usage
    control, _ = gate_for(models)
    result = control.drive("domain-process")
    assert result["status"] == "running" and result["needs_reconciliation"]
    node = next(n for n in result["result"]["nodes"] if n["id"] == "critic-model")
    assert node["status"] == "unknown" and node["reservation"]["output_tokens"] == 900
    assert counts()["messages"] == 1 and counts()["memories"] == 0
    before = list(calls)
    control.drive("domain-process")
    assert calls == before


@actual
@pytest.mark.parametrize("boundary", ["dispatched", "response", "journal"])
def test_model_process_boundaries_do_not_replay_provider(local_models, boundary):
    models, _ = local_models
    path = get_db_path()
    (path.parent / "model-identities.json").write_text(json.dumps(models.identities))

    def invoke(mode):
        return subprocess.run([sys.executable, "-m", "tests.local_model_driver", str(path), mode],
            cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(), capture_output=True,
            text=True, timeout=45, start_new_session=True)

    assert invoke(boundary).returncode == -signal.SIGKILL
    recovered = invoke("normal")
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout)
    assert len((path.parent / "provider-calls.jsonl").read_text().splitlines()) == 1
    if boundary == "dispatched":
        assert result["status"] == "running" and result["needs_reconciliation"]
        node = next(n for n in result["result"]["nodes"] if n["id"] == "critic-model")
        assert node["status"] == "unknown" and node["reservation"]["output_tokens"] == 900
        assert counts()["messages"] == 1
    else:
        assert result["status"] == "succeeded", result
        assert result["result"]["usage"]["input_tokens"] == 24
        assert result["result"]["usage"]["output_tokens"] == 8
        assert counts()["messages"] == 2
    observed = counts()
    assert invoke("normal").returncode == 0
    assert counts() == observed
    assert len((path.parent / "provider-calls.jsonl").read_text().splitlines()) == 1


@actual
@pytest.mark.parametrize("fault", ["unknown_usage", "malformed", "truncated"])
def test_invalid_provider_result_cannot_become_success(local_models, fault):
    models, _ = local_models
    original = models.transport
    def faulty(kind, payload, timeout):
        body = original(kind, payload, timeout)
        if kind == "embedding" and fault == "unknown_usage":
            body.pop("usage")
        if kind == "embedding" and fault == "malformed":
            body["data"] = []
        if kind == "llm" and fault == "truncated":
            body["choices"][0]["finish_reason"] = "length"
        return body
    models.transport = faulty
    control, _ = gate_for(models)
    if fault == "unknown_usage":
        with pytest.raises(BackendUnknown):
            control.drive("domain-process")
        result = control.drive("domain-process")
        assert result["status"] == "running" and result["needs_reconciliation"]
    else:
        assert control.drive("domain-process")["status"] == "failed"
    assert counts()["messages"] == 1 and counts()["memories"] == 0


@actual
def test_retrieval_measurements_cannot_change_after_completion(local_models):
    models, _ = local_models
    control, _ = gate_for(models)
    assert control.drive("domain-process")["status"] == "succeeded"
    with transaction() as conn:
        conn.execute("UPDATE host_backend_calls SET request_digest='changed' WHERE kind='reranker'")
    with pytest.raises(RequestConflictError, match="measurement proof"):
        control.rebuild_projection("domain-process")


def test_schema_16_migration_preserves_existing_receipts(local_models):
    models, _ = local_models
    control, _ = gate_for(models)
    before = counts()
    with transaction() as conn:
        conn.execute("DROP TABLE host_backend_calls")
        conn.execute("PRAGMA user_version=16")
    init_db(seed=False)
    init_db(seed=False)
    assert counts() == before
    assert control.status("domain-process")["status"] == "queued"
    with closing(connect()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
