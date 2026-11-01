"""Reproducible five-identity HTTP capacity check on new synthetic data only."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import platform
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.local_workflow_acceptance import check_package  # noqa: E402


@dataclass(frozen=True)
class Profile:
    documents: int = 100
    pages_per_document: int = 20
    evidence: int = 100
    transactions: int = 10_000
    users: int = 5
    reads_per_user: int = 100
    conflict_rounds: int = 20


THRESHOLDS: dict[str, Any] = {
    "scope": "local five-user technical pilot; not a contractual SLA",
    "read_p95_seconds": 1.5,
    "unexpected_failure_rate": 0.0,
    "export_p95_seconds": 10.0,
    "signing_step_seconds": 15.0,
    "api_peak_rss_bytes": 2 * 1024**3,
    "output_bytes": 2 * 1024**3,
    "remaining_free_bytes": 2 * 1024**3,
    "write_conflict": "exactly one 200 and one 409 per pair; winning content/version retained",
    "isolation": "all four scoped members receive 404 for an unassigned case; assistant/viewer cannot export",
    "correctness": "SQLite integrity/foreign keys and formal ZIP manifest hashes all valid",
}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("No observations")
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def timing(samples: list[dict[str, Any]]) -> dict[str, Any]:
    values = [item["seconds"] for item in samples]
    return {
        "count": len(values),
        "p50_seconds": percentile(values, 0.5),
        "p95_seconds": percentile(values, 0.95),
        "max_seconds": max(values),
        "unexpected_failures": sum(not item["expected"] for item in samples),
        "unexpected_failure_rate": sum(not item["expected"] for item in samples) / len(values),
    }


def hardware(path: Path) -> dict[str, Any]:
    memory: int | None = None
    if sys.platform == "darwin":
        memory = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    return {
        "os": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
        "memory_bytes": memory,
        "disk_free_bytes": shutil.disk_usage(path).free,
    }


def seed_data(directory: Path, profile: Profile = Profile()) -> dict[str, int]:  # noqa: B008 - frozen dataclass
    if directory.exists():
        raise ValueError("Synthetic data directory must be new")
    from app.db import db_scope, init_db, now, transaction

    with db_scope(directory / "law_review.db"):
        init_db(seed=False)
        stamp = now()
        with transaction() as conn:
            case_id = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", ("五人容量合成案", stamp, stamp)
            ).lastrowid
            other_id = conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)", ("未分配的隔离合成案", stamp, stamp)
            ).lastrowid
            first_document = 0
            for index in range(profile.documents):
                texts = [
                    f"合成合同 contract 卷宗{index + 1}第{page + 1}页：借款金额1200元，待人工核验。" + "x" * 1000
                    for page in range(profile.pages_per_document)
                ]
                payload = "\n\f\n".join(texts).encode("utf-8")
                name = f"synthetic-{index + 1:03}.txt"
                path = directory / "uploads" / name
                path.write_bytes(payload)
                document_id = conn.execute(
                    "INSERT INTO documents(case_id,name,stored_path,pages,content_hash,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (case_id, name, str(path), len(texts), hashlib.sha256(payload).hexdigest(), stamp, stamp),
                ).lastrowid
                first_document = first_document or document_id
                conn.executemany(
                    "INSERT INTO pages(document_id,page_no,text) VALUES (?,?,?)",
                    [(document_id, page + 1, text) for page, text in enumerate(texts)],
                )
            first_evidence = 0
            for index in range(profile.evidence):
                evidence_id = conn.execute(
                    "INSERT INTO evidence(case_id,title,fact,quote,created_at) VALUES (?,?,?,?,?)",
                    (case_id, f"合成证据{index + 1}", "合成借款事实", "合成合同引文", stamp),
                ).lastrowid
                first_evidence = first_evidence or evidence_id
            conn.executemany(
                "INSERT INTO bank_transactions(case_id,source_row_number,account,direction,amount_minor,currency,"
                "transaction_time,counterparty,memo,parser_version,row_fingerprint,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        case_id,
                        index + 1,
                        "合成账户",
                        "inflow" if index % 2 else "outflow",
                        1200,
                        "CNY" if index % 2 else "USD",
                        "2026-01-02",
                        "合成对方",
                        "合成流水",
                        "capacity-test",
                        f"synthetic-{index}",
                        stamp,
                    )
                    for index in range(profile.transactions)
                ],
            )
    return {
        "case_id": int(case_id),
        "other_case_id": int(other_id),
        "document_id": int(first_document),
        "evidence_id": int(first_evidence),
    }


class Client:
    def __init__(self, base: str, token: str, role: str) -> None:
        self.base, self.token, self.role = base, token, role

    def call(
        self, method: str, path: str, payload: Any = None, expected: tuple[int, ...] = (200,)
    ) -> tuple[dict[str, Any], bytes]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": "Bearer " + self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        started = time.perf_counter()
        try:
            with opener.open(req, timeout=30) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except OSError:
            status, body = 0, b""
        sample = {
            "role": self.role,
            "method": method,
            "path": path,
            "status": status,
            "seconds": time.perf_counter() - started,
            "expected": status in expected,
        }
        return sample, body

    def json(
        self, method: str, path: str, payload: Any = None, expected: tuple[int, ...] = (200,)
    ) -> tuple[dict[str, Any], Any]:
        sample, body = self.call(method, path, payload, expected)
        if not sample["expected"]:
            raise RuntimeError(f"Unexpected HTTP {sample['status']} for {method} {path}")
        return sample, json.loads(body)


@contextmanager
def api_process(data: Path, output: Path, token: str, rss: list[int]) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAW_REVIEW_") and key != "PYTHONPATH"}
    env.update(
        {
            "LAW_REVIEW_DATA_DIR": str(data),
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(data / "checkpoints.sqlite"),
            "LAW_REVIEW_ALLOWED_HOSTS": "localhost,127.0.0.1",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({token: {"name": "合成容量管理员", "admin": True}}),
            "LAW_REVIEW_LLM_URL": "http://127.0.0.1:1/v1",
            "LAW_REVIEW_EMBEDDING_URL": "http://127.0.0.1:1/v1",
            "LAW_REVIEW_RERANK_URL": "http://127.0.0.1:1/v1",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
        }
    )
    code = (
        "import app.main,uvicorn; print('LOADED',app.main.__file__,flush=True); "
        f"uvicorn.run(app.main.app,host='127.0.0.1',port={port})"
    )
    stopped = threading.Event()
    with (output / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
        )

        def monitor() -> None:
            while not stopped.wait(0.2):
                value = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True, check=False
                ).stdout.strip()
                if value:
                    rss.append(int(value) * 1024)

        observer = threading.Thread(target=monitor, daemon=True)
        observer.start()
        base = f"http://127.0.0.1:{port}"
        try:
            client = Client(base, token, "admin")
            for _ in range(120):
                if client.call("GET", "/api/health")[0]["status"] == 200:
                    break
                if process.poll() is not None:
                    raise RuntimeError("Isolated API exited before readiness")
                time.sleep(0.25)
            else:
                raise RuntimeError("Isolated API readiness timed out")
            if f"LOADED {ROOT / 'app/main.py'}" not in (output / "server.log").read_text():
                raise RuntimeError("API did not load current source")
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            stopped.set()
            observer.join(timeout=3)


def verify_package(
    body: bytes, expected_transactions: int, documents: int, report_id: int, preview: dict[str, Any], approved_by: str
) -> dict[str, Any]:
    try:
        check_package(body, preview, approved_by)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        manifest = json.loads(archive.read("清单.json"))
        if (
            not manifest["finalized"]
            or manifest["report"]["id"] != report_id
            or manifest["artifacts"][0]["transaction_count"] != expected_transactions
        ):
            raise ValueError("Formal package status or transaction count mismatch")
        if len(manifest["files"]) != documents:
            raise ValueError("Formal package attachment count mismatch")
        for item in manifest["files"] + manifest["artifacts"]:
            if hashlib.sha256(archive.read(item["archive_name"])).hexdigest() != item["sha256"]:
                raise ValueError("Formal package member hash mismatch")
        return {
            "report_id": manifest["report"]["id"],
            "content_hash": manifest["report"]["content_hash"],
            "attachments": len(manifest["files"]),
            "transactions": expected_transactions,
        }


def exercise(base: str, token: str, ids: dict[str, int], profile: Profile, output: Path) -> dict[str, Any]:
    case = ids["case_id"]
    admin = Client(base, token, "admin")
    clients = [admin]
    identities = []
    for role, permissions in (
        ("owner", ["view", "edit", "approve", "export"]),
        ("reviewer", ["view", "approve", "export"]),
        ("assistant", ["view", "edit"]),
        ("viewer", ["view"]),
    ):
        _, created = admin.json(
            "POST",
            "/api/members",
            {"name": "合成容量" + role, "permissions": permissions, "assignments": [{"case_id": case, "role": role}]},
            (201,),
        )
        clients.append(Client(base, created["token"], role))
    isolation = []
    for client in clients:
        _, login = client.json("POST", "/api/auth/session", {"token": client.token})
        identities.append({"role": client.role, "name": login["name"]})
        if client.role != "admin":
            for path in (f"/api/cases/{ids['other_case_id']}", f"/api/cases/{ids['other_case_id']}/export"):
                sample, _ = client.call("GET", path, expected=(404,))
                isolation.append(sample)
    owner, reviewer, assistant, viewer = clients[1:]
    paths = [
        f"/api/cases/{case}",
        f"/api/cases/{case}/documents",
        f"/api/cases/{case}/evidence",
        f"/api/documents/{ids['document_id']}/pages/1",
        f"/api/cases/{case}/search?q=contract",
        f"/api/cases/{case}/bank-transactions?limit=100",
        f"/api/cases/{case}/bank-transactions/summary",
        f"/api/cases/{case}/tasks",
        f"/api/cases/{case}/reports",
    ]
    # Explicit warmup is excluded from steady-state latency measurements.
    for path in paths:
        admin.json("GET", path)
    barrier = threading.Barrier(profile.users)

    def reads(client: Client) -> list[dict[str, Any]]:
        barrier.wait(timeout=10)
        return [client.call("GET", paths[index % len(paths)])[0] for index in range(profile.reads_per_user)]

    with ThreadPoolExecutor(max_workers=profile.users) as pool:
        read_samples = [sample for group in pool.map(reads, clients) for sample in group]
    conflicts: list[dict[str, Any]] = []
    for index in range(profile.conflict_rounds):
        _, listed = owner.json("GET", f"/api/cases/{case}/evidence")
        current = next(item for item in listed["evidence"] if item["id"] == ids["evidence_id"])
        start = threading.Barrier(2)

        def change(client: Client) -> tuple[dict[str, Any], Any]:
            start.wait(timeout=10)
            return client.json(
                "PATCH",
                f"/api/evidence/{ids['evidence_id']}",
                {"fact": f"合成并发轮次{index}-{client.role}", "expected_version": current["version"]},
                (200, 409),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            changed = list(pool.map(change, [owner, assistant]))
        if sorted(item[0]["status"] for item in changed) != [200, 409]:
            raise RuntimeError("Concurrent writes were not exactly one success and one conflict")
        winner = next(item[1]["evidence"] for item in changed if item[0]["status"] == 200)
        _, listed = owner.json("GET", f"/api/cases/{case}/evidence")
        stored = next(item for item in listed["evidence"] if item["id"] == ids["evidence_id"])
        if stored["version"] != current["version"] + 1 or stored["fact"] != winner["fact"]:
            raise RuntimeError("Winning write was overwritten or version changed unexpectedly")
        conflicts.extend(item[0] for item in changed)
    _, template = admin.json(
        "POST",
        "/api/export-templates",
        {
            "name": "合成容量模板",
            "blocks": [
                {"type": "case_summary", "title": "案件摘要"},
                {"type": "catalog_csv", "title": "目录"},
                {"type": "evidence_table", "title": "证据"},
                {"type": "attachments", "title": "附件", "required": True},
            ],
        },
        (201,),
    )
    _, report = owner.json(
        "POST", f"/api/cases/{case}/reports", {"title": "合成容量报告", "template_id": template["id"]}, (201,)
    )
    report_id = report["report"]["id"]
    submit, _ = owner.json("PATCH", f"/api/reports/{report_id}", {"action": "submit"})
    approve, _ = reviewer.json("PATCH", f"/api/reports/{report_id}", {"action": "approve"})
    _, frozen_preview = reviewer.json("GET", f"/api/reports/{report_id}/frozen")
    write_json(output / "frozen-preview.json", frozen_preview)
    export_path = f"/api/cases/{case}/export?final=true&report_id={report_id}"
    for client in (assistant, viewer):
        sample, _ = client.call("GET", export_path, expected=(403,))
        isolation.append(sample)
    start_export = threading.Barrier(profile.users)

    def exports(client: Client) -> tuple[dict[str, Any], dict[str, Any] | None]:
        start_export.wait(timeout=10)
        if client.role in {"admin", "owner", "reviewer"}:
            sample, body = client.call("GET", export_path)
            if not sample["expected"]:
                raise RuntimeError("Concurrent formal export failed")
            checked = verify_package(
                body, profile.transactions, profile.documents, report_id, frozen_preview, identities[2]["name"]
            )
            (output / f"formal-{client.role}.zip").write_bytes(body)
            return sample, checked
        return client.call("GET", f"/api/cases/{case}/reports")[0], None

    with ThreadPoolExecutor(max_workers=profile.users) as pool:
        delivered = list(pool.map(exports, clients))
    export_samples = [sample for sample, checked in delivered if checked is not None]
    if len({checked["content_hash"] for _, checked in delivered if checked is not None}) != 1:
        raise RuntimeError("Concurrent exports did not share approved content")
    write_json(
        output / "requests.json",
        {
            "reads": read_samples,
            "conflicts": conflicts,
            "export_phase": [sample for sample, _ in delivered],
            "isolation": isolation,
        },
    )
    return {
        "identities": identities,
        "reads": timing(read_samples),
        "conflicts": timing(conflicts),
        "conflict_rounds_passed": profile.conflict_rounds,
        "exports": timing(export_samples),
        "signing_steps_seconds": {"submit": submit["seconds"], "approve": approve["seconds"]},
        "isolation_passed": all(item["expected"] for item in isolation),
        "concurrent_readers_during_export_passed": all(sample["expected"] for sample, _ in delivered),
        "formal_package": next(checked for _, checked in delivered if checked is not None),
    }


def run(output: Path) -> dict[str, Any]:
    output = output.resolve()
    if output.exists():
        raise ValueError("Output must be a new directory; no existing data is opened or overwritten")
    output.mkdir(parents=True, mode=0o700)
    profile = Profile()
    write_json(output / "thresholds.json", {"profile": asdict(profile), "thresholds": THRESHOLDS})
    before = hardware(output)
    write_json(output / "hardware.json", before)
    report: dict[str, Any] = {
        "stage": "failed",
        "synthetic_only": True,
        "models_disabled": True,
        "ingestion_method": "direct synthetic fixture seeding; not upload/OCR throughput",
        "thresholds_sha256": hashlib.sha256((output / "thresholds.json").read_bytes()).hexdigest(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "candidate_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    }
    rss: list[int] = []
    try:
        ids = seed_data(output / "data", profile)
        token = secrets.token_urlsafe(48)
        with api_process(output / "data", output, token, rss) as base:
            report.update(exercise(base, token, ids, profile, output))
        database_path = output / "data/law_review.db"
        with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)) as db:
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("documents", "pages", "evidence", "bank_transactions")
            }
            report["database"] = {
                "counts": counts,
                "bytes": database_path.stat().st_size,
                "integrity": db.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                "foreign_keys": db.execute("PRAGMA foreign_key_check").fetchall() == [],
                "formal_delivery_events": db.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action='正式交付登记'"
                ).fetchone()[0],
            }
        report["data_counts_match"] = counts == {
            "documents": profile.documents,
            "pages": profile.documents * profile.pages_per_document,
            "evidence": profile.evidence,
            "bank_transactions": profile.transactions,
        }
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "reason": str(exc)}
    finally:
        report["api_peak_rss_bytes"] = max(rss, default=0)
        report["disk_free_after_bytes"] = shutil.disk_usage(output).free
        report["output_bytes"] = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
        if "failure" not in report:
            report["checks"] = evaluate(report)
            report["stage"] = "passed" if all(report["checks"].values()) else "failed"
        write_json(output / "result.json", report)
    return report


def evaluate(report: dict[str, Any]) -> dict[str, bool]:
    """Fixed gates are evaluated without changing them to match observations."""
    return {
        "read_latency": report["reads"]["p95_seconds"] <= THRESHOLDS["read_p95_seconds"],
        "read_failures": report["reads"]["unexpected_failure_rate"] <= THRESHOLDS["unexpected_failure_rate"],
        "conflict_failures": report["conflicts"]["unexpected_failure_rate"] == 0,
        "export_latency": report["exports"]["p95_seconds"] <= THRESHOLDS["export_p95_seconds"],
        "export_failures": report["exports"]["unexpected_failure_rate"] == 0,
        "signing_latency": max(report["signing_steps_seconds"].values()) <= THRESHOLDS["signing_step_seconds"],
        "isolation": report["isolation_passed"],
        "concurrent_readers": report["concurrent_readers_during_export_passed"],
        "rss": 0 < report["api_peak_rss_bytes"] <= THRESHOLDS["api_peak_rss_bytes"],
        "disk_budget": report["output_bytes"] <= THRESHOLDS["output_bytes"],
        "remaining_disk": report["disk_free_after_bytes"] >= THRESHOLDS["remaining_free_bytes"],
        "database": report["database"]["integrity"] and report["database"]["foreign_keys"],
        "data_counts": report["data_counts_match"],
        "delivery_count": report["database"]["formal_delivery_events"] == 3,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["stage"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
