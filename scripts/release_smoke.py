"""Release smoke: login, permissions, signing and ZIP download on a real process.

Run against an unpacked release started with token auth and an isolated data dir:

    uv run --locked python scripts/release_smoke.py --base-url http://127.0.0.1:18766 \
        --admin-token ADMIN --output /tmp/smoke-output

It creates only synthetic data, never touches the repository's data directory,
and prints one JSON summary so the release evidence can be archived. Requests use
stdlib HTTP with proxies disabled so a loopback instance is reached directly.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import io
import json
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )


def _request(opener: urllib.request.OpenerDirector, method: str, url: str, payload: Any = None,
             headers: dict[str, str] | None = None,
             raw: tuple[bytes, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    merged = dict(headers or {})
    data: bytes | None = None
    if raw is not None:
        data, merged["Content-Type"] = raw
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=merged)
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, response.read(), {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {key.lower(): value for key, value in exc.headers.items()}


def _multipart(field: str, filename: str, content: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = b"".join([
        b"--", boundary.encode(), b"\r\n",
        b'Content-Disposition: form-data; name="', field.encode(), b'"; filename="',
        filename.encode(), b'"\r\n',
        b"Content-Type: ", content_type.encode(), b"\r\n\r\n",
        content, b"\r\n",
        b"--", boundary.encode(), b"--\r\n",
    ])
    return body, f"multipart/form-data; boundary={boundary}"


def _text(body: bytes, limit: int = 200) -> str:
    return body[:limit].decode("utf-8", "replace")


def _fail(message: str) -> None:
    print(json.dumps({"stage": "failed", "reason": message}, ensure_ascii=False))
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--admin-token", required=True)
    parser.add_argument("--output", type=Path, default=None, help="optional directory to store the delivered ZIP")
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    steps: dict[str, str] = {}
    admin = _opener()
    reviewer = _opener()

    status, body, headers = _request(admin, "GET", base + "/")
    if status != 200 or b"<html" not in body.lower():
        _fail(f"前端首页未返回 HTML（{status}）")
    steps["static_ui"] = f"{len(body)} 字节，CSP={bool(headers.get('content-security-policy'))}"

    status, body, _ = _request(admin, "POST", base + "/api/auth/session", {"token": args.admin_token})
    if status != 200:
        _fail(f"管理员登录失败：{status} {_text(body)}")
    steps["admin_login"] = json.loads(body)["name"]

    # A rerun against the same instance must not collide with its own earlier data.
    suffix = uuid.uuid4().hex[:6]
    status, body, _ = _request(admin, "POST", base + "/api/members", {
        "name": f"复核律师-{suffix}", "permissions": ["view", "approve", "export"], "assignments": [],
    })
    if status != 201:
        _fail(f"创建复核成员失败：{status} {_text(body)}")
    member = json.loads(body)
    reviewer_token = member["token"]
    reviewer_id = member["member"]["id"]

    status, body, _ = _request(admin, "POST", base + "/api/cases", {
        "title": f"发布验收合成案-{suffix}", "case_no": f"(2026)验{suffix}号",
    })
    if status != 201:
        _fail(f"创建案件失败：{status} {_text(body)}")
    case_id = json.loads(body)["id"]
    status, body, _ = _request(admin, "PUT", f"{base}/api/cases/{case_id}/members/{reviewer_id}", {"role": "reviewer"})
    if status != 200:
        _fail(f"分配案件角色失败：{status} {_text(body)}")
    steps["case"] = f"案件 #{case_id}，复核成员 #{reviewer_id}"

    upload = _multipart("files", "借条.txt", "借条：今借到人民币十万元。".encode(), "text/plain")
    status, body, _ = _request(admin, "POST", f"{base}/api/cases/{case_id}/documents", raw=upload)
    if status != 201:
        _fail(f"上传材料失败：{status} {_text(body)}")
    steps["document"] = str(json.loads(body)["documents"][0]["id"])

    status, body, _ = _request(admin, "POST", f"{base}/api/cases/{case_id}/evidence", {
        "title": "借条原件", "category": "书证", "fact": "借款十万元",
        "quote": "今借到人民币十万元", "status": "已确认",
    })
    if status != 201:
        _fail(f"创建证据失败：{status} {_text(body)}")

    status, body, _ = _request(admin, "POST", base + "/api/export-templates", {
        "name": f"发布验收模板-{suffix}", "blocks": [
            {"type": "case_summary", "title": "案件摘要"},
            {"type": "evidence_table", "title": "证据目录"},
            {"type": "attachments", "title": "原始卷宗", "required": True},
        ],
    })
    if status != 201:
        _fail(f"创建模板失败：{status} {_text(body)}")
    template_id = json.loads(body)["id"]

    status, body, _ = _request(admin, "POST", f"{base}/api/cases/{case_id}/reports",
                               {"title": "发布验收报告", "template_id": template_id})
    if status != 201:
        _fail(f"创建报告失败：{status} {_text(body)}")
    report_id = json.loads(body)["report"]["id"]
    status, body, _ = _request(admin, "PATCH", f"{base}/api/reports/{report_id}", {"action": "submit"})
    if status != 200:
        _fail(f"提交复核失败：{status} {_text(body)}")
    frozen_id = json.loads(body)["report"]["frozen_version_id"]

    status, body, _ = _request(admin, "GET", f"{base}/api/reports/{report_id}/frozen")
    if status != 200 or not json.loads(body)["blocks"]:
        _fail(f"冻结预览不可用：{status}")
    preview = json.loads(body)
    steps["frozen"] = f"版本 #{frozen_id}，正文摘要 {preview['report']['content_hash'][:12]}"

    status, body, _ = _request(reviewer, "POST", base + "/api/auth/session", {"token": reviewer_token})
    if status != 200:
        _fail(f"复核律师登录失败：{status}")
    status, body, _ = _request(reviewer, "GET", f"{base}/api/cases/{case_id}/reports")
    if status != 200 or not json.loads(body)["reports"][0]["can_review"]:
        _fail("复核律师看不到可复核报告")
    status, body, _ = _request(reviewer, "POST", f"{base}/api/cases/{case_id}/reports", {"title": "越权草稿"})
    if status != 403:
        _fail(f"无 edit 权限的复核律师仍能新建报告（{status}）")
    status, body, _ = _request(reviewer, "PATCH", f"{base}/api/reports/{report_id}", {"action": "approve"})
    if status != 200:
        _fail(f"复核律师批准失败：{status} {_text(body)}")
    if not json.loads(body)["report"]["approved_by"].startswith("复核律师"):
        _fail("批准人归属不是复核律师")
    steps["reviewer_permission"] = "可复核并批准、不可编制"

    status, body, headers = _request(
        admin, "GET", f"{base}/api/cases/{case_id}/export?final=true&report_id={report_id}",
    )
    if status != 200:
        _fail(f"正式下载失败：{status} {_text(body)}")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        manifest = json.loads(archive.read("清单.json"))
        files = {name: archive.read(name) for name in archive.namelist()}
    if not manifest["finalized"] or manifest["report"]["id"] != report_id:
        _fail("交付包未绑定该报告")
    if manifest["frozen"]["version_id"] != frozen_id:
        _fail("交付包冻结版本与批准版本不一致")
    for item in manifest["files"]:
        if hashlib.sha256(files[item["archive_name"]]).hexdigest() != item["sha256"]:
            _fail(f"包内文件摘要不符：{item['archive_name']}")
    steps["delivery"] = (f"报告 #{report_id} v{manifest['report']['version']} "
                         f"正文摘要 {manifest['report']['content_hash'][:12]} "
                         f"包摘要 {headers.get('x-package-sha256', '')[:12]}")
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "delivery.zip").write_bytes(body)

    print(json.dumps({"stage": "passed", "base_url": base, "steps": steps}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
