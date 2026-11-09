"""Five independent synthetic identities on a real local API; no lawyer sign-off.

With no --base-url, create a disposable isolated instance and verify a stopped
backup/restore. For a pre-existing disposable instance, supply --base-url and
--synthetic-instance, with its administrator token in the named environment
variable. --verify-restored reuses only this tool's synthetic state/credentials.
Credentials stay in the private ignored output directory, never the journal.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ops.data_snapshot import backup, restore, verify  # noqa: E402
from scripts.ops.migration_demo import server  # noqa: E402
from scripts.ops.release_smoke import _multipart, _opener, _request  # noqa: E402

ROLES = {
    "owner": ["view", "edit", "approve", "export"],
    "reviewer": ["view", "approve", "export"],
    "assistant": ["view", "edit"],
    "viewer": ["view"],
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def local_url(value: str) -> str:
    url = urlsplit(value)
    require(
        url.scheme in {"http", "https"}
        and url.hostname in {"127.0.0.1", "localhost", "::1"}
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment
        and url.path in {"", "/"},
        "Only a loopback API origin is supported",
    )
    return value.rstrip("/")


def write_json(path: Path, payload: Any, *, private: bool = False) -> None:
    with path.open("x", encoding="utf-8") as stream:
        if private:
            os.chmod(path, 0o600)
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def check_package(package: bytes, preview: dict[str, Any], approved_by: str) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = json.loads(archive.read("清单.json"))
        report = manifest["report"]
        require(manifest["finalized"] and report["id"] == preview["report"]["id"], "Wrong formal report")
        require(report["content_hash"] == preview["report"]["content_hash"], "Frozen body hash mismatch")
        require(report["approved_by"] == approved_by, "Approval does not belong to the reviewer")
        require(manifest["frozen"]["version_id"] == preview["report"]["frozen_version_id"], "Frozen version mismatch")
        names = archive.namelist()
        require(len(names) == len(set(names)), "Duplicate package member")
        attachments = {item["archive_name"]: item for item in preview["attachments"]}
        declared = {item["archive_name"]: item for item in manifest["files"]}
        require(len(declared) == len(manifest["files"]), "Duplicate attachment manifest entry")
        require(set(declared) == set(attachments), "Frozen attachment set mismatch")
        for name, expected in attachments.items():
            require(declared[name]["sha256"] == expected["sha256"], "Frozen attachment digest mismatch")
            require(declared[name]["size"] == expected["size"], "Frozen attachment size mismatch")
        sheet = preview["transactions"]
        require(len(manifest["artifacts"]) == 1, "Frozen transaction artifact missing")
        artifact = manifest["artifacts"][0]
        require(artifact["archive_name"] == sheet["archive_name"], "Wrong transaction sheet")
        require(artifact["sha256"] == sheet["sha256"], "Frozen transaction digest mismatch")
        require(artifact["transaction_count"] == sheet["count"], "Frozen transaction count mismatch")
        expected_names = (
            {item["archive_name"] for item in preview["blocks"]} | set(attachments) | {sheet["archive_name"]}
        )
        require(set(names) == expected_names | {"清单.json"}, "Frozen package member set mismatch")
        members = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names}
        for item in manifest["files"] + manifest["artifacts"] + preview["blocks"]:
            require(members[item["archive_name"]] == item["sha256"], "Package member digest mismatch")
        with (
            archive.open(sheet["archive_name"]) as raw,
            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text,
        ):
            rows = csv.reader(text)
            header = next(rows, [])
            require(len(header) == 9, "Transaction CSV header mismatch")
            actual_count = 0
            for row in rows:
                require(len(row) == len(header), "Malformed transaction CSV row")
                actual_count += 1
        require(actual_count == sheet["count"], "Actual transaction CSV row count mismatch")
        return {name: digest for name, digest in members.items() if name != "清单.json"}


class Session:
    def __init__(self, base: str, role: str, journal: list[dict[str, Any]]) -> None:
        self.base, self.role, self.journal = local_url(base), role, journal
        self.opener = _opener()

    def call(
        self,
        method: str,
        path: str,
        payload: Any = None,
        expected: int | tuple[int, ...] = 200,
        *,
        raw: tuple[bytes, str] | None = None,
    ) -> Any:
        started = time.monotonic()
        status, body, _ = _request(self.opener, method, self.base + path, payload, raw=raw)
        self.journal.append(
            {
                "identity": self.role,
                "method": method,
                "path": path,
                "status": status,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
        allowed = (expected,) if isinstance(expected, int) else expected
        # Never include a response body or submitted token in failure reports.
        require(status in allowed, f"{self.role} {method} {path}: expected {allowed}, got {status}")
        if status in {200, 201} and body.startswith((b"{", b"[")):
            return json.loads(body)
        return body

    def login(self, token: str) -> dict[str, Any]:
        return self.call("POST", "/api/auth/session", {"token": token})

    def upload(self, case_id: int, filename: str, text: str) -> dict[str, Any]:
        body = _multipart("files", filename, text.encode(), "text/plain")
        result = self.call("POST", f"/api/cases/{case_id}/documents", expected=201, raw=body)
        require(len(result["documents"]) == 1 and not result.get("failures"), "Synthetic upload did not complete")
        return result["documents"][0]


def exercise(base: str, admin_token: str, output: Path, journal: list[dict[str, Any]]) -> dict[str, Any]:
    suffix = secrets.token_hex(4)
    sessions = {role: Session(base, role, journal) for role in ("admin", *ROLES)}
    admin, owner, reviewer, assistant, viewer = (sessions[role] for role in ("admin", *ROLES))
    admin_identity = admin.login(admin_token)
    require(admin_identity["admin"], "Bootstrap identity must be an administrator")
    case_id = admin.call("POST", "/api/cases", {"title": f"五身份合成验收-{suffix}"}, 201)["id"]
    other_id = admin.call("POST", "/api/cases", {"title": f"隔离对照合成案-{suffix}"}, 201)["id"]
    credentials = {"admin": {"name": admin_identity["name"], "token": admin_token}}
    for role, permissions in ROLES.items():
        member = admin.call("POST", "/api/members", {"name": f"合成-{role}-{suffix}", "permissions": permissions}, 201)
        credentials[role] = {"name": member["member"]["name"], "token": member["token"], "id": member["member"]["id"]}
        admin.call("PUT", f"/api/cases/{case_id}/members/{member['member']['id']}", {"role": role})
        identity = sessions[role].login(member["token"])
        require(identity["name"] == member["member"]["name"], "Independent identity login mismatch")
        require(sessions[role].call("GET", "/api/auth/me")["name"] == identity["name"], "Session identity mismatch")

    owner.call(
        "PATCH", f"/api/cases/{case_id}", {"description": "合成借款卷宗：金额1200元，需补签字页", "expected_version": 1}
    )
    doc = assistant.upload(case_id, "合成借条.txt", "借条：今借到人民币1200元。此为合成验收材料。")
    other_doc = admin.upload(other_id, "隔离材料.txt", "仅隔离对照案可见的合成内容。")
    for role in ROLES:
        for path in (
            f"/api/cases/{other_id}",
            f"/api/documents/{other_doc['id']}/file",
            f"/api/documents/{other_doc['id']}/pages/1",
            f"/api/cases/{other_id}/export",
        ):
            sessions[role].call("GET", path, expected=404)
    viewer.call("PATCH", f"/api/cases/{case_id}", {"description": "只读越权修改"}, 403)
    viewer.call("POST", f"/api/cases/{case_id}/tasks", {"title": "只读越权任务"}, 403)
    viewer.call("GET", f"/api/cases/{case_id}/export", expected=403)
    reviewer.call("POST", f"/api/cases/{case_id}/reports", {"title": "复核人越权编制"}, 403)

    # Two clients keep the same old material/evidence versions: reject the second save.
    corrected = owner.call(
        "PATCH", f"/api/documents/{doc['id']}/directory", {"people": "合成借款人", "expected_version": doc["version"]}
    )
    assistant.call(
        "PATCH",
        f"/api/documents/{doc['id']}/directory",
        {"people": "陈旧目录覆盖", "expected_version": doc["version"]},
        409,
    )
    evidence = assistant.call(
        "POST",
        f"/api/cases/{case_id}/evidence",
        {
            "title": "合成借条金额",
            "category": "书证",
            "fact": "借款1200元，待复核",
            "quote": "今借到人民币1200元",
            "source_document_id": doc["id"],
            "source_page_start": 1,
            "source_page_end": 1,
        },
        201,
    )["evidence"]
    evidence = owner.call(
        "PATCH",
        f"/api/evidence/{evidence['id']}",
        {"fact": "承办已核对金额1200元", "expected_version": evidence["version"]},
    )["evidence"]
    assistant.call("PATCH", f"/api/evidence/{evidence['id']}", {"fact": "陈旧覆盖", "expected_version": 1}, 409)
    approved = reviewer.call(
        "PATCH", f"/api/evidence/{evidence['id']}", {"status": "已确认", "expected_version": evidence["version"]}
    )["evidence"]
    require(approved["approved_by"] == credentials["reviewer"]["name"], "Evidence approval attribution mismatch")
    correction = assistant.call(
        "PATCH",
        f"/api/documents/{doc['id']}/pages/1",
        {
            "text": "借条：今借到人民币壹仟贰佰元。此为合成验收材料。",
            "reason": "合成OCR纠错",
            "expected_version": corrected["version"],
        },
    )
    require(
        evidence["id"] in [row["id"] for row in correction["invalidated"]], "Changed quote did not invalidate approval"
    )
    owner.call(
        "PATCH",
        f"/api/documents/{doc['id']}/pages/1",
        {"text": "陈旧原文覆盖", "expected_version": corrected["version"]},
        409,
    )
    latest = next(
        row for row in reviewer.call("GET", f"/api/cases/{case_id}/evidence")["evidence"] if row["id"] == evidence["id"]
    )
    require(latest["status"] == "待复核" and not latest["approved_by"], "Broken quote retained confirmation")
    updated = assistant.call(
        "PATCH",
        f"/api/evidence/{evidence['id']}",
        {"quote": "今借到人民币壹仟贰佰元", "expected_version": latest["version"]},
    )["evidence"]
    reviewer.call(
        "PATCH", f"/api/evidence/{evidence['id']}", {"status": "已确认", "expected_version": updated["version"]}
    )

    task = owner.call(
        "POST",
        f"/api/cases/{case_id}/tasks",
        {
            "title": "补充合成签字页",
            "kind": "补证",
            "assignee_member_id": credentials["assistant"]["id"],
            "due_at": "2026-12-31",
        },
        201,
    )["task"]

    def task_action(who: Session, action: str, **extra: Any) -> None:
        nonlocal task
        task = who.call(
            "PATCH", f"/api/tasks/{task['id']}", {"action": action, "expected_version": task["version"], **extra}
        )["task"]

    task_action(assistant, "start")
    task_action(assistant, "submit")
    reviewer.call("PATCH", f"/api/tasks/{task['id']}", {"action": "return", "note": " "}, 400)
    # A retained review role must not restore withdrawn approve permission.
    reviewer_id = credentials["reviewer"]["id"]
    admin.call("PATCH", f"/api/members/{reviewer_id}", {"permissions": ["view"]})
    reviewer.call("PATCH", f"/api/tasks/{task['id']}", {"action": "complete"}, 403)
    admin.call("PATCH", f"/api/members/{reviewer_id}", {"permissions": ROLES["reviewer"]})
    owner_id = credentials["owner"]["id"]
    admin.call("PATCH", f"/api/members/{owner_id}", {"permissions": ["view"]})
    owner.call("PATCH", f"/api/tasks/{task['id']}", {"action": "complete"}, 403)
    admin.call("PATCH", f"/api/members/{owner_id}", {"permissions": ROLES["owner"]})
    task_action(reviewer, "return", note="签字页不清楚，请补传")
    require(task["events"][-1]["note"] == "签字页不清楚，请补传", "Task return reason missing")
    assistant.upload(case_id, "合成签字页.txt", "合成签字页：借款人签名，金额壹仟贰佰元。")
    task_action(assistant, "start")
    task_action(assistant, "submit")
    task_action(reviewer, "complete", note="合成补证复核完成")
    require(
        [row["action"] for row in task["events"]]
        == ["create", "start", "submit", "return", "start", "submit", "complete"],
        "Task event history mismatch",
    )
    owner.call("PATCH", f"/api/tasks/{task['id']}", {"action": "start", "expected_version": task["version"]}, 409)

    blocks: list[dict[str, Any]] = [
        {"type": "case_summary", "title": "案件摘要"},
        {"type": "catalog_csv", "title": "卷宗目录"},
        {"type": "evidence_table", "title": "证据目录"},
        {"type": "qa_log", "title": "问答记录"},
        {"type": "static_markdown", "title": "合成声明", "content": "本包仅用于合成技术验收。"},
        {"type": "attachments", "title": "原始材料", "required": True},
    ]
    template_payload = {"name": f"五身份模板-{suffix}", "blocks": blocks}
    template_id = admin.call("POST", "/api/export-templates", template_payload, 201)["id"]

    def new_report() -> dict[str, Any]:
        report = owner.call(
            "POST", f"/api/cases/{case_id}/reports", {"title": "合成五身份报告", "template_id": template_id}, 201
        )["report"]
        return owner.call("PATCH", f"/api/reports/{report['id']}", {"action": "submit"})["report"]

    def export_path(report: dict[str, Any]) -> str:
        return f"/api/cases/{case_id}/export?final=true&report_id={report['id']}"

    report = new_report()
    viewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"}, 403)
    assistant.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"}, 403)
    admin.call("PATCH", f"/api/members/{owner_id}", {"permissions": ["view"]})
    owner.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"}, 403)
    admin.call("PATCH", f"/api/members/{owner_id}", {"permissions": ROLES["owner"]})
    admin.call("PATCH", f"/api/members/{reviewer_id}", {"permissions": ["view"]})
    reviewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"}, 403)
    admin.call("PATCH", f"/api/members/{reviewer_id}", {"permissions": ROLES["reviewer"]})
    preview = reviewer.call("GET", f"/api/reports/{report['id']}/frozen")
    approved_report = reviewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"})["report"]
    first = owner.call("GET", export_path(report))
    check_package(first, preview, credentials["reviewer"]["name"])
    (output / "initial-delivery.zip").write_bytes(first)
    # Template selection, static text and QA mutations each invalidate delivery.
    owner.call("GET", export_path(report) + "&template_id=1", expected=400)
    blocks[4]["content"] = "静态正文已调整，须重新冻结复核。"
    admin.call("PUT", f"/api/export-templates/{template_id}", template_payload)
    owner.call("GET", export_path(report), expected=400)
    report = new_report()
    reviewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"})
    chat = assistant.call(
        "POST",
        f"/api/cases/{case_id}/chat",
        {
            "question": "统计卷宗数量",
            "use_llm": False,
            "use_remote_embeddings": False,
        },
    )
    owner.call("GET", export_path(report), expected=400)
    report = new_report()
    reviewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"})
    assistant.call("DELETE", f"/api/conversations/{chat['conversation_id']}")
    owner.call("GET", export_path(report), expected=400)
    # A submitted version cannot be approved after additional material arrives.
    report = new_report()
    assistant.upload(case_id, "合成补充说明.txt", "补充材料导致待复核版本依据变化。")
    reviewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"}, 409)
    report = new_report()
    reviewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"})
    latest = next(
        row for row in owner.call("GET", f"/api/cases/{case_id}/evidence")["evidence"] if row["id"] == evidence["id"]
    )
    assistant.call(
        "PATCH",
        f"/api/evidence/{evidence['id']}",
        {"fact": "合成材料新增说明待核对", "expected_version": latest["version"]},
    )
    owner.call("GET", export_path(report), expected=400)
    report = new_report()
    reviewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"})
    bank = "账号,交易日期,交易金额,收支方向,对方户名,币种\n合成账户,2026-10-29,1200,收入,合成对方,CNY\n"
    owner.call(
        "POST",
        f"/api/cases/{case_id}/bank-transactions/import",
        expected=201,
        raw=_multipart("file", "合成流水.csv", bank.encode(), "text/csv"),
    )
    owner.call("GET", export_path(report), expected=400)

    # Restore all five working identities after explicit revoke/disable/rotation tests.
    assistant_id = credentials["assistant"]["id"]
    admin.call("PATCH", f"/api/members/{assistant_id}", {"active": False})
    assistant.call("GET", f"/api/cases/{case_id}", expected=401)
    Session(base, "disabled-token", journal).call(
        "POST", "/api/auth/session", {"token": credentials["assistant"]["token"]}, 401
    )
    admin.call("PATCH", f"/api/members/{assistant_id}", {"active": True})
    rotated = admin.call("POST", f"/api/members/{assistant_id}/tokens", {}, 201)
    credentials["assistant"]["token"] = rotated["token"]
    assistant.login(rotated["token"])
    viewer_id, old_token = credentials["viewer"]["id"], credentials["viewer"]["token"]
    rotated = admin.call("POST", f"/api/members/{viewer_id}/tokens", {}, 201)
    viewer.call("GET", f"/api/cases/{case_id}", expected=401)
    Session(base, "rotated-token", journal).call("POST", "/api/auth/session", {"token": old_token}, 401)
    credentials["viewer"]["token"] = rotated["token"]
    viewer.login(rotated["token"])
    latest = next(
        row for row in reviewer.call("GET", f"/api/cases/{case_id}/evidence")["evidence"] if row["id"] == evidence["id"]
    )
    reviewer.call(
        "PATCH", f"/api/evidence/{evidence['id']}", {"status": "已确认", "expected_version": latest["version"]}
    )
    report = new_report()
    preview = reviewer.call("GET", f"/api/reports/{report['id']}/frozen")
    approved_report = reviewer.call("PATCH", f"/api/reports/{report['id']}", {"action": "approve"})["report"]
    package = owner.call("GET", export_path(report))
    contents = check_package(package, preview, credentials["reviewer"]["name"])
    (output / "final-delivery.zip").write_bytes(package)
    viewer.call("GET", f"/api/reports/{report['id']}/frozen")
    assistant.call("GET", export_path(report), expected=403)
    viewer.call("GET", export_path(report), expected=403)
    # The hidden case gets a frozen version; all non-admin identities still get 404.
    hidden_report = admin.call("POST", f"/api/cases/{other_id}/reports", {}, 201)["report"]
    admin.call("PATCH", f"/api/reports/{hidden_report['id']}", {"action": "submit"})
    for role in ROLES:
        sessions[role].call("GET", f"/api/reports/{hidden_report['id']}/frozen", expected=404)
    for guessed in ("/report_versions/body.json", "/assets/../report_versions/body.json"):
        viewer.call("GET", guessed, expected=404)
    audits = owner.call("GET", f"/api/cases/{case_id}/audit")
    require(any("任务" in row["action"] for row in audits), "Task audit missing")
    require(any("报告" in row["action"] for row in audits), "Report audit missing")
    state = {
        "synthetic_only": True,
        "case_id": case_id,
        "other_case_id": other_id,
        "document_id": doc["id"],
        "evidence_id": evidence["id"],
        "task_id": task["id"],
        "report": approved_report,
        "preview": preview,
        "delivery_members": contents,
    }
    write_json(output / "credentials.json", credentials, private=True)
    write_json(output / "state.json", state)
    return {
        "case_id": case_id,
        "identity_count": 5,
        "roles": ["admin", *ROLES],
        "task_events": len(task["events"]),
        "report_id": report["id"],
        "frozen_version_id": report["frozen_version_id"],
        "content_hash": approved_report["content_hash"],
        "delivery_members": len(contents),
        "synthetic_only": True,
        "lawyer_signoff": False,
        "covered_technical_scenarios": ["A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08"],
        "not_covered": [
            "A09 export-time concurrent fault injection",
            "A10 capacity and missing attachments",
            "A11 storage faults",
            "A12 v13/v25 migration",
            "A13 real model/legal review",
            "A14 capacity/concurrency threshold",
            "A15 operator response",
        ],
    }


def verify_restored(base: str, prior: Path, output: Path, journal: list[dict[str, Any]]) -> dict[str, Any]:
    state = json.loads((prior / "state.json").read_text())
    require(state["synthetic_only"] is True, "Only synthetic acceptance state is supported")
    credentials = json.loads((prior / "credentials.json").read_text())
    sessions = {role: Session(base, role, journal) for role in ("admin", *ROLES)}
    case_id, report = state["case_id"], state["report"]
    for role, session in sessions.items():
        identity = session.login(credentials[role]["token"])
        require(identity["name"] == credentials[role]["name"], "Restored login identity mismatch")
        require(session.call("GET", f"/api/cases/{case_id}")["id"] == case_id, "Restored case inaccessible")
    owner, assistant, reviewer, viewer = (sessions[role] for role in ("owner", "assistant", "reviewer", "viewer"))
    preview = reviewer.call("GET", f"/api/reports/{report['id']}/frozen")
    package = owner.call("GET", f"/api/cases/{case_id}/export?final=true&report_id={report['id']}")
    require(
        check_package(package, preview, credentials["reviewer"]["name"]) == state["delivery_members"],
        "Restored approved body or attachment differs",
    )
    (output / "restored-delivery.zip").write_bytes(package)
    task = owner.call(
        "POST",
        f"/api/cases/{case_id}/tasks",
        {
            "title": "恢复后补证处理验证",
            "assignee_member_id": credentials["assistant"]["id"],
        },
        201,
    )["task"]
    for session, action in ((assistant, "start"), (assistant, "submit"), (reviewer, "complete")):
        task = session.call(
            "PATCH", f"/api/tasks/{task['id']}", {"action": action, "expected_version": task["version"]}
        )["task"]
    viewer.call("PATCH", f"/api/tasks/{task['id']}", {"action": "start"}, 403)
    require(task["status"] == "已完成", "Restored workflow not operable")
    return {
        "identity_logins": 5,
        "approved_report_id": report["id"],
        "delivery_members_matched": len(state["delivery_members"]),
        "new_task_id": task["id"],
        "new_task_status": task["status"],
        "synthetic_only": True,
    }


def run(
    output: Path, *, base_url: str | None = None, admin_token: str = "", restored_from: Path | None = None
) -> dict[str, Any]:
    output = output.resolve()
    require(not output.exists(), "Output must be a new directory")
    if base_url:
        local_url(base_url)
    output.mkdir(parents=True, mode=0o700)
    journal: list[dict[str, Any]] = []
    started = time.monotonic()
    result: dict[str, Any] = {"stage": "failed", "synthetic_only": True, "lawyer_signoff": False}
    try:
        if restored_from:
            require(bool(base_url), "Restored verification requires a local base URL")
            result["restored"] = verify_restored(base_url or "", restored_from, output, journal)
        elif base_url:
            require(bool(admin_token), "Missing administrator token environment variable")
            result["workflow"] = exercise(base_url, admin_token, output, journal)
        else:
            admin_token = secrets.token_urlsafe(48)
            inherited = {key: value for key, value in os.environ.items() if not key.startswith("LAW_REVIEW_")}
            env = dict(
                inherited,
                LAW_REVIEW_AUTH_MODE="token",
                LAW_REVIEW_API_TOKENS_JSON=json.dumps({admin_token: {"name": "合成本机验收管理员", "admin": True}}),
                LAW_REVIEW_LLM_URL="http://127.0.0.1:1/v1",
                LAW_REVIEW_EMBEDDING_URL="http://127.0.0.1:1/v1",
                LAW_REVIEW_RERANK_URL="http://127.0.0.1:1/v1",
                REDIS_URL="redis://127.0.0.1:1/0",
                LANGSMITH_TRACING="false",
                LANGCHAIN_TRACING_V2="false",
            )
            with server(output / "data", env, output / "server.log") as base:
                result["workflow"] = exercise(base, admin_token, output, journal)
            backup(output / "data", output / "snapshot", quiescent=True)
            verify(output / "snapshot")
            restore(output / "snapshot", output / "restored-data")
            with server(output / "restored-data", env, output / "restored-server.log") as base:
                result["restored"] = verify_restored(base, output, output, journal)
        result["stage"] = "passed"
    except Exception as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result.update(
            {
                "http_requests": len(journal),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "candidate_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        )
        write_json(output / "http-journal.json", journal)
        write_json(output / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url")
    parser.add_argument(
        "--synthetic-instance", action="store_true", help="Confirm the given API is disposable and isolated"
    )
    parser.add_argument("--admin-token-env", default="LAW_REVIEW_ACCEPTANCE_ADMIN_TOKEN")
    parser.add_argument("--verify-restored", type=Path)
    args = parser.parse_args(argv)
    require(not args.base_url or args.synthetic_instance, "Confirm an external API is a disposable synthetic instance")
    result = run(
        args.output,
        base_url=args.base_url,
        admin_token=os.getenv(args.admin_token_env, ""),
        restored_from=args.verify_restored,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
