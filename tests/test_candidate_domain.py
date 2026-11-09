from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from app.core.security import Principal, _principal
from app.integration.candidate_domain import CandidateDeployment, CandidateDomainIntegration
from app.integration.domain_integration import IsolatedDomainIntegration
from app.integration.host_writes import create_isolated_database
from app.integration.runtime_ports import (
    AccessDeniedError,
    ContractValidationError,
    ModelConfig,
    RequestConflictError,
    RunScope,
)
from app.storage.db import connect, db_scope, init_db, now, transaction
from tests.test_domain_integration import counts, request
from tests.test_domain_writes import domain  # noqa: F401
from tests.test_host_writes import isolated  # noqa: F401
from tests.test_local_backends import local_models  # noqa: F401

actual = pytest.mark.skipif(not os.getenv("LAW_REVIEW_FORGE_PYTHON"), reason="installed framework Python3.12 required")


@pytest.fixture
def candidate(local_models, tmp_path):
    models, calls = local_models
    deployment = CandidateDeployment.create_new(tmp_path / "acceptance" / "host.sqlite")
    with db_scope(deployment.database):
        with transaction() as conn:
            conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('Synthetic candidate',?,?)", (now(), now()))
            conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('Outside deployment',?,?)", (now(), now()))
            doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
                "VALUES (1,'合成记录','','text/plain',1,?,?)", (now(), now())).lastrowid
            conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,'合成审批记录，固定回报需要核验。')", (doc,))
        yield CandidateDomainIntegration(deployment=deployment, models=models), calls


def candidate_request(control, **changes):
    changes.setdefault("model", ModelConfig(use_llm=True))
    return request(runtime=control.runtime, **changes)


@actual
def test_candidate_writes_unmarked_db_and_refuses_synthetic_relabel(candidate):
    control, _ = candidate
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM host_integration_config").fetchone()[0] == 0
    with pytest.raises(AccessDeniedError, match="synthetic"):
        IsolatedDomainIntegration()
    accepted = control.accept(candidate_request(control, model=ModelConfig(use_llm=True), use_remote_embeddings=True))
    assert control.drive(accepted["request_id"])["status"] == "succeeded"
    observed = counts()
    assert observed["messages"] == 2 and observed["memories"] == 1
    assert control.rebuild_projection(accepted["request_id"])["status"] == "succeeded"
    assert counts() == observed
    init_db(seed=False, recover_runs=True)
    assert control.status(accepted["request_id"])["status"] == "succeeded"


def test_new_candidate_factory_cannot_adopt_existing_db_or_enable_production(candidate, tmp_path):
    control, _ = candidate
    with pytest.raises(FileExistsError):
        CandidateDeployment.create_new(control.path)
    with pytest.raises(ContractValidationError):
        replace(control.deployment, environment="production")
    path = tmp_path / "synthetic.sqlite"
    create_isolated_database(path)
    stat = path.stat()
    config = replace(control.deployment, database=path, file_identity=(stat.st_dev, stat.st_ino))
    with db_scope(path), pytest.raises(AccessDeniedError, match="relabel"):
        CandidateDomainIntegration(deployment=config, models=control.models)


def test_host_configuration_and_case_scope_are_bound(candidate):
    control, calls = candidate
    with pytest.raises(AccessDeniedError, match="scope"):
        control.accept(candidate_request(control, scope=RunScope(2)))
    assert not calls and counts()["agent_runs"] == 0
    control.accept(candidate_request(control))
    changed = CandidateDomainIntegration(deployment=replace(control.deployment, timeout_seconds=301), models=control.models)
    with pytest.raises(RequestConflictError, match="deployment"):
        changed.status("domain-process")
    path = control.deployment.database
    old = path.with_name("original.sqlite")
    path.rename(old)
    path.write_bytes(b"replaced")
    with pytest.raises(AccessDeniedError, match="replaced"):
        control.status("domain-process")


@actual
@pytest.mark.parametrize("pages,extra,allowed", [(500,0,True),(501,0,False),(500,1,False)])
def test_candidate_confirmed_full_body_capacity(candidate, pages, extra, allowed):
    control, _ = candidate
    with transaction() as conn:
        conn.execute("DELETE FROM pages")
        conn.executemany("INSERT INTO pages(document_id,page_no,text) VALUES (1,?,?)",
            [(i+1, "合成审批记录" + "字" * (3994 + (extra if i == 0 else 0))) for i in range(pages)])
    if allowed:
        control.accept(candidate_request(control))
        assert control.drive("domain-process")["status"] == "succeeded"
    else:
        with pytest.raises(ContractValidationError, match="capacity"):
            control.accept(candidate_request(control))
        assert counts()["agent_runs"] == 0


@actual
def test_candidate_revocation_archive_and_deletion(candidate, monkeypatch):
    control, _ = candidate
    token = "candidate-token-" + "x" * 32
    config = {token: {"name": "候选律师", "case_ids": [1], "permissions": ["view", "edit"]}}
    monkeypatch.setenv("LAW_REVIEW_AUTH_MODE", "token")
    monkeypatch.setenv("LAW_REVIEW_API_TOKENS_JSON", json.dumps(config))
    marker = _principal.set(Principal("候选律师", False, (1,), permissions=frozenset({"view", "edit"})))
    try:
        control.accept(candidate_request(control))
        config[token]["permissions"] = ["view"]
        monkeypatch.setenv("LAW_REVIEW_API_TOKENS_JSON", json.dumps(config))
        assert control.status("domain-process")["status"] == "queued"
        with pytest.raises(AccessDeniedError):
            control.drive("domain-process")
    finally:
        _principal.reset(marker)
    monkeypatch.setenv("LAW_REVIEW_AUTH_MODE", "local")
    with transaction() as conn:
        conn.execute("UPDATE cases SET lifecycle_status='archived' WHERE id=1")
    with pytest.raises(AccessDeniedError, match="read-only"):
        control.accept(candidate_request(control, request_id="archived"))
    with transaction() as conn:
        conn.execute("DELETE FROM cases WHERE id=1")
    with pytest.raises(AccessDeniedError):
        control.status("domain-process")
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM host_backend_calls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM host_write_receipts").fetchone()[0] == 0
    assert control.purge_deleted_state() == 1
    assert control.purge_deleted_state() == 0


@actual
@pytest.mark.parametrize("boundary", ["accept", "dispatched", "response", "journal"])
@pytest.mark.parametrize("refused", [False, True])
def test_candidate_new_process_keeps_scope_identity_and_no_replay(candidate, boundary, refused):
    control, _ = candidate
    path = control.path
    if refused:
        (path.parent / "provider-refusal").touch()
    config = asdict(control.deployment)
    config["database"] = str(config["database"])
    (path.parent / "candidate-deployment.json").write_text(json.dumps(config))
    (path.parent / "model-identities.json").write_text(json.dumps(control.models.identities))
    def invoke(mode):
        return subprocess.run([sys.executable, "-m", "tests.local_model_driver", str(path), mode],
            cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(), capture_output=True,
            text=True, timeout=45, start_new_session=True)
    assert invoke(boundary).returncode == -signal.SIGKILL
    recovered = invoke("normal")
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout)
    assert result["status"] == ("running" if boundary == "dispatched" else "failed" if refused else "succeeded"), result
    assert result["needs_reconciliation"] == (boundary == "dispatched")
    assert len((path.parent / "provider-calls.jsonl").read_text().splitlines()) == 1
    observed = counts()
    assert invoke("normal").returncode == 0
    assert counts() == observed
