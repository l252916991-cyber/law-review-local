"""Verify that the source release package can rebuild the framework binding.

B3 acceptance (docs/history/migration/status-and-improvement-plan-20261021.md): from
the actual release archive, in an isolated environment, rebuild the framework
binding, verify digests and run a contract smoke check - without depending on
development-machine absolute paths or unpackaged local files.

Steps:
1. Build the release archive from the current allowlist (or accept --zip).
2. Extract to a temporary directory.
3. Verify the vendored framework wheel's sha256 against vendor/FRAMEWORK_BINDING.json.
4. Create an isolated venv, install the wheel from the extracted tree.
5. Smoke: import forgeharness + SDK surface from the installed wheel, and
   exercise app/runtime_ports.py from the extracted tree (stdlib-only, so it
   runs on the framework venv without LexVault dependencies).
6. Install a fresh host venv from the extracted hashed requirements.
7. Run readonly, isolated host, and candidate success/refusal flows from the
   extracted tree; repeat host processes to prove durable result reuse.

Network (or a warm uv cache) is required for step 4 dependency resolution.

Usage:
    uv run --locked python -m scripts.verify_deployment_release
    uv run --locked python -m scripts.verify_deployment_release --zip dist/lexvault-source.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BINDING_RELATIVE = "vendor/FRAMEWORK_BINDING.json"

_SMOKE_PROGRAM = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import forgeharness
import forgeharness.sdk as sdk
binding = json.loads(sys.argv[2])
assert forgeharness.__version__ == binding["version_label"], (
    f"installed {forgeharness.__version__} != binding {binding['version_label']}"
)
for name in ("Harness", "RunKernel", "Orchestrator", "RunContext", "RunResult",
             "RunBudget", "compose_application", "Capability", "DurableGraph", "DurableNode",
             "DurableOrchestrator", "ReconciledNode"):
    assert hasattr(sdk, name), name
from forgeharness.applications.legal.lexvault import (
    BundleCitationVerifier, CaseBundle, LexVaultDagOrchestrator, bundle_from_lexvault_db,
)
from app.runtime_ports import (
    RUNTIME_PROTOCOL_VERSION, ActorIdentity, ModelConfig, RunRequest, RunScope,
    compute_request_digest, new_request_id,
)
request = RunRequest(
    request_id=new_request_id(),
    scope=RunScope(case_id=1),
    actor=ActorIdentity("本机律师", admin=True, local=True),
    question="部署冒烟：契约可用性检查",
    model=ModelConfig(use_llm=False),
)
request.validate()
digest = compute_request_digest(request, principal_name="本机律师", effective_author="本机律师")
assert len(digest) == 64
print(json.dumps({
    "forgeharness": forgeharness.__version__,
    "sdk_api": sdk.API_VERSION,
    "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
    "request_digest_prefix": digest[:12],
}))
"""


_M2_PROGRAM = r"""
import json, os, sys
from pathlib import Path
from contextlib import closing
release = Path(sys.argv[1])
sys.path.insert(0, str(release))
os.environ.update({"LAW_REVIEW_DATA_DIR": sys.argv[3], "LAW_REVIEW_AUTH_MODE": "local",
                   "LAW_REVIEW_LEGAL_CORPUS_DIR": "", "LAW_REVIEW_LLM_URL": "http://127.0.0.1:1/v1"})
import app
assert Path(app.__file__).resolve().is_relative_to(release.resolve())
from app.db import connect, init_db
from app.forge_bridge import run_framework
from app.forge_readonly import ReadonlyCaseSession
from app.runtime_adapter import current_actor_identity
from app.runtime_ports import ModelConfig, RunRequest, RunScope, new_request_id
init_db(seed=True)
with closing(connect()) as conn:
    before = tuple(conn.iterdump())
request = RunRequest(request_id=new_request_id(), scope=RunScope(1), actor=current_actor_identity(),
                     runtime="forgeharness-readonly", question="核对固定回报审批矛盾和证据疏漏",
                     model=ModelConfig(use_llm=False), use_remote_embeddings=False, persist_memory=False)
with ReadonlyCaseSession(request) as session:
    report = run_framework(session, Path(sys.argv[2]))
assert report["status"] == "succeeded", report["error"]
assert report["comparison"]["passed"]
with closing(connect()) as conn:
    assert before == tuple(conn.iterdump())
print(json.dumps({"status": report["status"], "domain_comparison": True,
                  "source_unchanged": True, "source_tree": "extracted release",
                  "host_dependencies": "fresh host venv from extracted hashed requirements",
                  "framework_dependencies": "fresh separate venv from hashed runtime requirements"}))
"""


_HOST_PROGRAM = r"""
import json, os, sys
from pathlib import Path
from contextlib import closing
release = Path(sys.argv[1])
sys.path.insert(0, str(release))
os.environ.update({"LAW_REVIEW_AUTH_MODE": "local", "LAW_REVIEW_DATA_DIR": sys.argv[3]})
import app
assert Path(app.__file__).resolve().is_relative_to(release.resolve())
from app.db import connect, db_scope, now, transaction
from app.host_writes import create_isolated_database, ISOLATED_RUNTIME
from app.host_integration import IsolatedHostIntegration
from app.runtime_adapter import current_actor_identity
from app.runtime_ports import RunRequest, RunScope, ModelConfig
path = Path(sys.argv[3]) / 'host.sqlite'
if not path.exists():
    create_isolated_database(path)
    with db_scope(path), transaction() as conn:
        conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('Synthetic release smoke',?,?)", (now(),now()))
with db_scope(path):
    control = IsolatedHostIntegration(framework_python=Path(sys.argv[2]))
    request = RunRequest(request_id='release-write-smoke',scope=RunScope(1),actor=current_actor_identity(),
        question='Synthetic persistence check',runtime=ISOLATED_RUNTIME,
        model=ModelConfig(use_llm=False),use_remote_embeddings=False)
    accepted = control.accept(request)
    result = control.drive(request.request_id)
    assert result['status'] == 'succeeded'
    assert result['result']['usage']['tool_calls'] == 6
    with closing(connect()) as conn:
        for table, expected in [('agent_runs',1),('conversations',1),('messages',2),('memories',1),
                                ('gap_detections',1),('agent_steps',1),('audit_log',1),('host_write_receipts',7)]:
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == expected, table
    print(json.dumps({'source_tree':'extracted release','status':result['status'],'run_id':result['run_id'],
        'framework_run_id':result['framework_run_id'],'duplicate':accepted['duplicate'],'result':result['result']}))
"""


_CANDIDATE_PROGRAM = r"""
import json, os, sys, hashlib
from pathlib import Path
from dataclasses import asdict
from contextlib import closing
release, framework, root = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(release))
root.mkdir(parents=True, exist_ok=True)
os.environ.update({'LAW_REVIEW_DATA_DIR':str(root), 'LAW_REVIEW_AUTH_MODE':'local',
                   'LAW_REVIEW_LEGAL_CORPUS_DIR':'', 'LAW_REVIEW_LLM_URL':'http://127.0.0.1:1/v1'})
import app
assert Path(app.__file__).resolve().is_relative_to(release.resolve())
from app.answerability import policy_identity
from app.candidate_domain import CandidateDeployment, CandidateDomainIntegration
from app.db import connect, db_scope, transaction, now
from app.host_writes import digest
from app.local_backends import LocalModels
from app.runtime_adapter import current_actor_identity
from app.runtime_ports import ModelConfig, RunRequest, RunScope
config = root / 'deployment.json'
if not config.exists():
    deployment = CandidateDeployment.create_new(root / 'host.sqlite')
    values = asdict(deployment)
    values['database'] = str(values['database'])
    config.write_text(json.dumps(values))
    with db_scope(deployment.database), transaction() as conn:
        conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('Synthetic release case',?,?)",(now(),now()))
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
            "VALUES (1,'合成审批记录','','text/plain',1,?,?)",(now(),now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,'合成审批记录：该转款仍需核验审批。')",(doc,))
values = json.loads(config.read_text())
values['database'] = Path(values['database'])
values['file_identity'],values['case_ids'] = tuple(values['file_identity']),tuple(values['case_ids'])
deployment = CandidateDeployment(**values)
identities = {}
for name in ('llm','embedding','reranker'):
    folder = root / ('fixture-' + name)
    folder.mkdir(exist_ok=True)
    entries = {}
    for filename,body in [('config.json',b'{"synthetic":true}'),('model.safetensors',b'release protocol fixture')]:
        path = folder / filename
        if not path.exists(): path.write_bytes(body)
        entries[filename] = {'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                             'bytes':path.stat().st_size,'mtime_ns':path.stat().st_mtime_ns}
    identities[name] = {'path':str(folder),'files':entries,'identity':'local-sha256:' + digest(entries)}
calls = root / 'provider-calls.jsonl'
def transport(kind,payload,timeout):
    assert kind == 'llm'
    with calls.open('a') as stream: stream.write(json.dumps({'kind':kind}) + '\n')
    user_content = payload['messages'][1]['content']
    question = user_content.split('\n', 1)[0].removeprefix('问题：')
    refusal = question == '验证无关材料拒绝'
    content = {'decision':'unrelated' if refusal else 'supported','reason':'release protocol fixture, not a semantic measurement',
               'findings':[] if refusal else [{'text':'审批记录尚需核验。','sources':[1]}]}
    return {'model':'llm','choices':[{'finish_reason':'stop','message':{'content':json.dumps(content,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':24,'completion_tokens':8}}
models = LocalModels(endpoint='http://127.0.0.1:1/v1', identities=identities,
                     models={k:k for k in identities},transport=transport)
with db_scope(deployment.database):
    control = CandidateDomainIntegration(deployment=deployment,models=models,framework_python=framework)
    results = []
    for name,question,expected in [('positive','合成审批记录','succeeded'),('refusal','验证无关材料拒绝','failed')]:
        request = RunRequest(request_id=name,scope=RunScope(1),actor=current_actor_identity(),question=question,
            runtime=control.runtime,model=ModelConfig(use_llm=True),use_remote_embeddings=False,persist_memory=False)
        accepted = control.accept(request)
        result = control.drive(name)
        assert result['status'] == expected and not result['needs_reconciliation'],result
        results.append({'duplicate':accepted['duplicate'],'framework_run_id':result['framework_run_id'],
                        'result':result['result']})
    with closing(connect()) as conn:
        failed = json.loads(conn.execute("SELECT output_json FROM agent_steps WHERE node_name='finish' AND status='failed'").fetchone()[0])['value']
        assert failed['diagnostic']['code'] == 'answerability_rejected'
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        counts = {name:conn.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] for name in
                  ('agent_runs','agent_steps','messages','memories','gap_detections','host_write_receipts')}
assert len(calls.read_text().splitlines()) == 2
print(json.dumps({'results':results,'counts':counts,'provider_calls':2,'policy':policy_identity(),
                  'source_tree':'extracted release','measurement_valid':False}))
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


def build_archive(destination: Path) -> Path:
    from scripts.package_release import main as package_main

    exit_code = package_main(["--output", str(destination)])
    if exit_code != 0:
        raise RuntimeError("package_release failed")
    return destination


def verify(archive: Path) -> dict[str, Any]:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to create the isolated verification venv")
    report: dict[str, Any] = {"archive": str(archive), "archive_sha256": _sha256(archive)}

    with tempfile.TemporaryDirectory(prefix="lexvault-deploy-verify-") as temporary:
        workdir = Path(temporary)
        extracted = workdir / "release"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extracted)

        binding_path = extracted / BINDING_RELATIVE
        if not binding_path.is_file():
            raise RuntimeError(f"release archive lacks {BINDING_RELATIVE}")
        binding = json.loads(binding_path.read_text())
        requirements = extracted / "vendor/forgeharness-runtime-requirements.txt"
        if _sha256(requirements) != binding["runtime_requirements_sha256"]:
            raise RuntimeError("framework runtime requirements digest mismatch")
        wheel_name = f"forgeharness-{binding['version_label']}-py3-none-any.whl"
        wheel_path = extracted / "vendor" / wheel_name
        if not wheel_path.is_file():
            raise RuntimeError(f"release archive lacks vendor/{wheel_name}")

        wheel_sha256 = _sha256(wheel_path)
        if wheel_sha256 != binding["wheel_sha256"]:
            raise RuntimeError(
                f"wheel digest mismatch: {wheel_sha256} != binding {binding['wheel_sha256']}"
            )
        report["binding"] = {
            "version_label": binding["version_label"],
            "wheel_sha256": wheel_sha256,
            "source_identity": binding.get("source_identity"),
            "sdk_api": binding.get("sdk_api"),
        }

        venv = workdir / "venv"
        _run([uv, "venv", str(venv), "--python", "3.12"])
        _run([uv, "pip", "install", "--python", str(venv / "bin" / "python"),
              "--require-hashes", "-r", str(requirements)])
        _run([uv, "pip", "install", "--python", str(venv / "bin" / "python"), "--no-deps", str(wheel_path)])
        _run([uv, "pip", "check", "--python", str(venv / "bin" / "python")])
        smoke = _run(
            [
                str(venv / "bin" / "python"), "-c", _SMOKE_PROGRAM,
                str(extracted), json.dumps(binding),
            ]
        )
        report["smoke"] = json.loads(smoke.stdout.strip().splitlines()[-1])
        host_venv = workdir / "host-venv"
        _run([uv, "venv", str(host_venv), "--python", "3.12"])
        host_python = str(host_venv / "bin/python")
        _run([uv, "pip", "install", "--python", host_python, "--require-hashes",
              "-r", str(extracted / "requirements.txt")])
        _run([uv, "pip", "check", "--python", host_python])
        report["fresh_host_requirements_sha256"] = _sha256(extracted / "requirements.txt")
        m2 = _run([host_python, "-I", "-c", _M2_PROGRAM, str(extracted),
                   str(venv / "bin/python"), str(workdir / "synthetic-data")], cwd=workdir)
        report["m2_extracted_source_smoke"] = json.loads(m2.stdout.strip().splitlines()[-1])
        host_command = [host_python, "-I", "-c", _HOST_PROGRAM, str(extracted),
                        str(venv / "bin/python"), str(workdir / "isolated-host-data")]
        first = json.loads(_run(host_command, cwd=workdir).stdout.strip().splitlines()[-1])
        restarted = json.loads(_run(host_command, cwd=workdir).stdout.strip().splitlines()[-1])
        if (first["duplicate"] or not restarted["duplicate"]
                or first["framework_run_id"] != restarted["framework_run_id"]
                or first["result"] != restarted["result"]):
            raise RuntimeError("extracted host restart changed the durable result")
        report["isolated_host_extracted_source_smoke"] = {
            "status": restarted["status"], "new_host_process_reused_result": True,
            "unique_effects_and_receipts": True, "engineering_only": True,
        }
        candidate_command = [host_python, "-I", "-c", _CANDIDATE_PROGRAM, str(extracted),
                             str(venv / "bin/python"), str(workdir / "candidate-data")]
        candidate_first = json.loads(_run(candidate_command, cwd=workdir).stdout.strip().splitlines()[-1])
        candidate_restarted = json.loads(_run(candidate_command, cwd=workdir).stdout.strip().splitlines()[-1])
        if (any(r["duplicate"] for r in candidate_first["results"])
                or not all(r["duplicate"] for r in candidate_restarted["results"])
                or candidate_first["counts"] != candidate_restarted["counts"]
                or any(a["framework_run_id"] != b["framework_run_id"] or a["result"] != b["result"]
                       for a, b in zip(candidate_first["results"], candidate_restarted["results"]))):
            raise RuntimeError("extracted candidate restart changed decisions, calls or effects")
        report["candidate_extracted_source_smoke"] = {"supported": True, "explicit_refusal": True,
            "new_host_process_reused_results": True, "provider_calls": candidate_restarted["provider_calls"],
            "unique_effects_and_receipts": True, "measurement_valid": False,
            "policy": candidate_restarted["policy"]}
        report["result"] = "PASS"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", type=Path, default=None,
                        help="existing release archive (default: build a fresh one into a temp dir)")
    args = parser.parse_args(argv)

    if args.zip is not None:
        report = verify(args.zip.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="lexvault-release-build-") as temporary:
            archive = build_archive(Path(temporary) / "lexvault-source.zip")
            report = verify(archive)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
