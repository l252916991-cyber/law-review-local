"""Exercise a deployed LexVault with one synthetic case and verify its export.

Run only against a disposable data volume: the public API can move the case to
trash, but intentionally does not permanently delete it or its audit history.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from typing import Any


SYNTHETIC_TEXT = (
    "合成卷宗第一页：双方于2026年9月1日签订测试合同，本页不记载付款金额。"
    "\f合成卷宗第二页：事项编号 ZXQ-914，乙方应于2026年9月9日向甲方支付1200元。"
)
QUESTION = "事项编号 ZXQ-914 的付款金额是多少？请说明证据来源。"


class SmokeError(Exception):
    """A delivery acceptance step failed without exposing case materials."""


class Client:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 240.0) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            raise SmokeError("--base-url must be an HTTP(S) origin without a path")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, body: bytes | None = None,
                content_type: str | None = None) -> tuple[bytes, dict[str, str]]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            raise SmokeError(f"{method} {path} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SmokeError(f"{method} {path} could not connect: {type(exc.reason).__name__}") from exc

    def json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        raw, _ = self.request(method, path, body, "application/json" if body is not None else None)
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeError(f"{method} {path} did not return JSON") from exc
        if not isinstance(result, dict):
            raise SmokeError(f"{method} {path} returned an unexpected JSON shape")
        return result


def multipart_text(name: str, text: str) -> tuple[bytes, str]:
    boundary = "lexvault-smoke-" + uuid.uuid4().hex
    content = text.encode("utf-8")
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
        f"filename=\"{name}\"\r\nContent-Type: text/plain\r\n\r\n"
    ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


def verify_export(payload: bytes, expected_sha: str, document_id: int, source_sha: str) -> None:
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != expected_sha:
        raise SmokeError("export SHA-256 differs from X-Package-Sha256")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            manifest = json.loads(archive.read("清单.json"))
            if not any(item.get("id") == document_id for item in manifest["documents"]):
                raise SmokeError("export manifest omits the uploaded document")
            files = manifest.get("files", [])
            if not isinstance(files, list) or not any(
                item.get("document_id") == document_id and item.get("sha256") == source_sha for item in files
            ):
                raise SmokeError("export manifest omits the original uploaded file or its SHA-256")
            for item in [*files, *manifest.get("artifacts", [])]:
                name = item["archive_name"]
                if hashlib.sha256(archive.read(name)).hexdigest() != item["sha256"]:
                    raise SmokeError(f"export manifest hash mismatch for {name}")
    except (KeyError, TypeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise SmokeError("export package or manifest is invalid") from exc


def run(client: Client, mode: str) -> dict[str, Any]:
    health = client.json("GET", "/api/health")
    if health.get("status") != "ok":
        raise SmokeError("API health is not ok")
    case_id: int | None = None
    try:
        created = client.json("POST", "/api/cases", {
            "title": "交付验收合成案件 " + uuid.uuid4().hex[:8],
            "case_no": "SYNTHETIC-ONLY", "description": "仅用于隔离环境交付验收",
        })
        case_id = created.get("id")
        if type(case_id) is not int:
            raise SmokeError("create case response omits integer id")
        body, content_type = multipart_text("synthetic-review.txt", SYNTHETIC_TEXT)
        raw, _ = client.request("POST", f"/api/cases/{case_id}/documents", body, content_type)
        uploaded = json.loads(raw)
        documents = uploaded.get("documents", [])
        if uploaded.get("failures") or len(documents) != 1 or type(documents[0].get("id")) is not int:
            raise SmokeError("synthetic upload was not indexed as one document")
        document_id = documents[0]["id"]
        page = client.json("GET", f"/api/documents/{document_id}/pages/2")
        if page.get("page_no") != 2 or "ZXQ-914" not in page.get("text", ""):
            raise SmokeError("the synthetic second page cannot be located")
        answer = client.json("POST", f"/api/cases/{case_id}/chat", {
            "question": QUESTION,
            "use_llm": mode == "model",
            "use_remote_embeddings": mode == "model",
        })
        citations = answer.get("citations", [])
        if not isinstance(citations, list) or not any(
            item.get("document_id") == document_id and item.get("page") == 2 for item in citations
        ):
            raise SmokeError("answer does not cite the source document's second page")
        if mode == "model" and answer.get("llm_used") is not True:
            raise SmokeError("model mode did not generate a validated LLM answer")
        if mode == "model" and not any(amount in answer.get("answer", "") for amount in ("1200", "1,200")):
            raise SmokeError("model answer omits the synthetic payment amount")
        package, headers = client.request("GET", f"/api/cases/{case_id}/export")
        package_sha = headers.get("X-Package-Sha256", headers.get("x-package-sha256", ""))
        verify_export(package, package_sha, document_id,
                      hashlib.sha256(SYNTHETIC_TEXT.encode("utf-8")).hexdigest())
        return {"case_id": case_id, "document_id": document_id, "cited_page": 2,
                "model_used": mode == "model", "export_sha256": package_sha}
    finally:
        if case_id is not None:
            try:
                client.json("POST", f"/api/cases/{case_id}/trash")
            except SmokeError as exc:
                print(f"Warning: synthetic case {case_id} could not be moved to trash: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mode", choices=("offline", "model"), default="offline")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    try:
        result = run(Client(args.base_url, os.getenv("LAW_REVIEW_SMOKE_TOKEN"), args.timeout), args.mode)
    except (SmokeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Delivery smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
