"""Reproduce a synthetic historical-schema migration and recovery; no source DB input."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.data_snapshot import backup, restore, verify  # noqa: E402

BASELINE = "092ecacf56621251184035f6aa05b03f5bbe5b6c"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summary(directory: Path) -> dict[str, Any]:
    with closing(sqlite3.connect((directory / "law_review.db").as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        return {
            "schema": db.execute("PRAGMA user_version").fetchone()[0],
            "cases": db.execute("SELECT id,title,case_no,description FROM cases ORDER BY id").fetchall(),
            "documents": db.execute("SELECT id,case_id,name,content_hash FROM documents ORDER BY id").fetchall(),
            "pages": db.execute("SELECT id,document_id,page_no,text FROM pages ORDER BY id").fetchall(),
            "evidence": db.execute("SELECT id,case_id,title,quote,status FROM evidence ORDER BY id").fetchall(),
            "runs": db.execute("SELECT id,case_id,status,checkpoint_thread_id FROM agent_runs ORDER BY id").fetchall(),
        }


def checkpoint_state(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        return {
            "rows": db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0],
            "logical_sha256": hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest(),
        }


def package_contents(package: bytes, report_id: int, content_hash: str, version_id: int) -> dict[str, str]:
    """Check restored delivery and return all member digests for before/after comparison."""
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = json.loads(archive.read("清单.json"))
        if not manifest["finalized"] or manifest["report"]["id"] != report_id:
            raise ValueError("Formal delivery report mismatch")
        if manifest["report"]["content_hash"] != content_hash or manifest["frozen"]["content_hash"] != content_hash:
            raise ValueError("Formal delivery content hash mismatch")
        if manifest["frozen"]["version_id"] != version_id or manifest["report"]["frozen_version_id"] != version_id:
            raise ValueError("Formal delivery frozen version mismatch")
        digests = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()}
        for item in manifest["files"] + manifest["artifacts"]:
            if digests[item["archive_name"]] != item["sha256"]:
                raise ValueError("Formal delivery member digest mismatch")
        return {name: digest for name, digest in digests.items() if name != "清单.json"}


def command(args: list[str], cwd: Path, env: dict[str, str], log: Path) -> None:
    with log.open("w") as stream:
        result = subprocess.run(args, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
    if result.returncode:
        raise RuntimeError(f"Command failed; inspect {log.name}")


def request(base: str, path: str, token: str) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
    with opener.open(req, timeout=30) as response:
        return response.read()


@contextmanager
def server(directory: Path, env: dict[str, str], log: Path) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    configured = dict(
        env,
        LAW_REVIEW_DATA_DIR=str(directory),
        LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB=str(directory / "langgraph_checkpoints.sqlite"),
    )
    code = (
        "import app.main,uvicorn; print('LOADED',app.main.__file__,flush=True); "
        f"uvicorn.run(app.main.app,host='127.0.0.1',port={port})"
    )
    with log.open("w") as stream:
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=ROOT, env=configured, stdout=stream, stderr=subprocess.STDOUT
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(60):
                try:
                    request(base, "/api/health", "")
                    break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError(f"Service exited; inspect {log.name}") from None
                    time.sleep(0.25)
            else:
                raise RuntimeError("Service readiness timed out")
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    assert str(ROOT / "app/main.py") in log.read_text()


def run(output: Path) -> dict[str, Any]:
    if not __debug__:
        raise RuntimeError("Run without Python optimization so all drill assertions execute")
    output = output.resolve()
    if output.exists():
        raise ValueError("Output must be a new directory; existing data is never used or overwritten")
    output.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    baseline = output / "baseline-code"
    baseline.mkdir()
    archive = subprocess.check_output(["git", "archive", BASELINE, "app"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Unexpected historical source archive member")
        tar.extractall(baseline)
    source = output / "synthetic-v16"
    checkpoint = output / "external-checkpoint.sqlite"
    token = secrets.token_urlsafe(48)
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LAW_REVIEW_") and key not in {"PYTHONPATH", "PYTHONOPTIMIZE"}
    }
    env = dict(
        inherited,
        LAW_REVIEW_DATA_DIR=str(source),
        LAW_REVIEW_AUTH_MODE="token",
        LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB=str(checkpoint),
        LAW_REVIEW_API_TOKENS_JSON=json.dumps(
            {
                token: {
                    "name": "Synthetic migration admin",
                    "admin": True,
                    "permissions": ["view", "edit", "approve", "export", "manage"],
                }
            }
        ),
        LAW_REVIEW_LLM_URL="http://127.0.0.1:1/v1",
        LAW_REVIEW_EMBEDDING_URL="http://127.0.0.1:1/v1",
        LAW_REVIEW_RERANK_URL="http://127.0.0.1:1/v1",
        REDIS_URL="redis://127.0.0.1:1/0",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
    )
    seed = (
        "from pathlib import Path; import os; from app.db import init_db; init_db(seed=True); "
        "from app.services import index_upload; "
        "index_upload(1,'migration-synthetic.txt','合成迁移样本：金额1200元。'.encode(),'text/plain'); "
        "from app.langgraph_agents import LangGraphCoordinator; "
        "LangGraphCoordinator(1,False,None,Path(os.environ['LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB']))"
        ".process_query('梳理证据及待核验事项',use_llm=False)"
    )
    command([sys.executable, "-c", seed], baseline, dict(env, LAW_REVIEW_AUTH_MODE="local"), output / "seed.log")
    before = summary(source)
    assert before["schema"] == 16
    checkpoints = checkpoint_state(checkpoint)
    assert checkpoints["rows"] > 0
    original = {"database": sha(source / "law_review.db"), "checkpoint": sha(checkpoint)}
    backup(source, output / "before-snapshot", checkpoint=checkpoint, quiescent=True)
    verify(output / "before-snapshot")
    restore(output / "before-snapshot", output / "migration-copy")
    with server(output / "migration-copy", env, output / "migration-server.log") as base:
        migrated = summary(output / "migration-copy")
        from app.db import SCHEMA_VERSION

        assert migrated["schema"] == SCHEMA_VERSION
        assert all(migrated[key] == before[key] for key in before if key != "schema")
        assert checkpoint_state(output / "migration-copy/langgraph_checkpoints.sqlite") == checkpoints
        assert json.loads(request(base, "/api/cases/1", token))["id"] == 1
        command(
            [
                sys.executable,
                "-m",
                "scripts.release_smoke",
                "--base-url",
                base,
                "--admin-token",
                token,
                "--output",
                str(output / "synthetic-export"),
            ],
            ROOT,
            env,
            output / "release-smoke.log",
        )
    upgraded = summary(output / "migration-copy")
    with closing(sqlite3.connect((output / "migration-copy/law_review.db").as_uri() + "?mode=ro", uri=True)) as db:
        report_id, case_id, content_hash, version_id = db.execute(
            "SELECT id,case_id,content_hash,approved_version_id FROM case_reports WHERE status='已批准' ORDER BY id LIMIT 1"
        ).fetchone()
    delivered = package_contents(
        (output / "synthetic-export/delivery.zip").read_bytes(), report_id, content_hash, version_id
    )
    backup(output / "migration-copy", output / "after-snapshot", quiescent=True)
    verify(output / "after-snapshot")
    restore(output / "after-snapshot", output / "second-restore")
    with server(output / "second-restore", env, output / "restored-server.log") as base:
        restored = summary(output / "second-restore")
        assert restored == upgraded
        assert checkpoint_state(output / "second-restore/langgraph_checkpoints.sqlite") == checkpoints
        preview = json.loads(request(base, f"/api/reports/{report_id}/frozen", token))
        assert preview["report"]["content_hash"] == content_hash
        package = request(base, f"/api/cases/{case_id}/export?final=true&report_id={report_id}", token)
        assert package_contents(package, report_id, content_hash, version_id) == delivered
        (output / "restored-formal-export.zip").write_bytes(package)
    restore(output / "before-snapshot", output / "rollback-copy")
    command(
        [sys.executable, "-c", "from app.db import init_db; init_db(seed=False)"],
        baseline,
        dict(env, LAW_REVIEW_DATA_DIR=str(output / "rollback-copy")),
        output / "rollback.log",
    )
    assert summary(output / "rollback-copy") == before
    assert checkpoint_state(output / "rollback-copy/langgraph_checkpoints.sqlite") == checkpoints
    assert original == {"database": sha(source / "law_review.db"), "checkpoint": sha(checkpoint)}
    result = {
        "stage": "passed",
        "synthetic_only": True,
        "real_database_migration": False,
        "baseline_commit": BASELINE,
        "candidate_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "driver_sha256": sha(Path(__file__)),
        "source_schema": before["schema"],
        "target_schema": migrated["schema"],
        "preserved_cases": len(before["cases"]),
        "preserved_documents": len(before["documents"]),
        "preserved_pages": len(before["pages"]),
        "preserved_evidence": len(before["evidence"]),
        "preserved_runs": len(before["runs"]),
        "checkpoint": checkpoints,
        "external_checkpoint_carried": True,
        "restored_approved_report_id": report_id,
        "restored_content_hash": content_hash,
        "rollback_schema": 16,
        "source_unchanged": True,
        "restored_delivery_members_matched": len(delivered),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
