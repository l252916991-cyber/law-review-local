"""Real /chat acceptance on a new synthetic case and a previously frozen protocol.

Requires an explicitly disposable loopback instance. Credentials are read from
an environment variable, held in memory, and excluded from journals/results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.local_workflow_acceptance import ROLES, Session, local_url, require  # noqa: E402


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(
        r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?",
        lambda m: f"{int(m[1])}-{int(m[2])}-{int(m[3])}",
        text,
    )
    return re.sub(r"[\s,，]", "", text)


NUMBER_CHARS = "零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億负負"
VALUE_BOUNDARY = re.escape("0123456789abcdefghijklmnopqrstuvwxyz_.-−" + NUMBER_CHARS)


def known_value_in_text(text: str, value: str) -> bool:
    """Match a complete numeric/date/identifier value; words retain literal matching.

    Reject obvious signed numbers and immediate numeric negation. This is a
    narrow frozen-value guard, not a semantic or legal entailment classifier.
    """
    content, expected = normalize(text), normalize(value)
    numeric = any(char.isdigit() or char in NUMBER_CHARS for char in expected)
    if not numeric:
        return expected in content
    pattern = rf"(?<![{VALUE_BOUNDARY}]){re.escape(expected)}(?![{VALUE_BOUNDARY}])"
    for match in re.finditer(pattern, content):
        prefix = re.sub(r"[：:「」“”‘’'\"（）()]", "", content[max(0, match.start() - 12):match.start()])
        if not prefix.endswith(("不是", "并非", "而非", "不为", "不等于", "并不是")):
            return True
    return False


def assess_answer(item: dict[str, Any], response: dict[str, Any], documents: dict[str, Any],
                  fixtures: dict[str, Any]) -> dict[str, Any]:
    """Check frozen known values and their actual cited pages, not general entailment."""
    answer = str(response.get("answer", ""))
    citations = response.get("citations") or []
    scope = {"semantic_entailment_checked": False, "manual_review_required": True}
    if item["kind"] == "missing" and response.get("answer_status") == "insufficient_evidence":
        valid = ("资料不足" in answer and not citations and not response.get("llm_used")
                 and response.get("validation", {}).get("valid") is True)
        return {"passed": valid, "safe_nonfactual_refusal": valid, "issues": [] if valid else ["invalid_refusal"], **scope}
    issues = []
    if not response.get("llm_used"):
        issues.append("real_llm_not_used")
    if response.get("citation_check") != "passed":
        issues.append("product_citation_check_failed")
    for alternatives in item["required_any"]:
        if not any(known_value_in_text(answer, value) for value in alternatives):
            issues.append("missing_required_known_value")
    if any(known_value_in_text(answer, value) for value in item.get("forbidden", [])):
        issues.append("unsupported_known_value")
    ids = {document["id"]: key for key, document in documents.items()}
    referenced = {int(value) for value in re.findall(r"[\[【［]\s*资料\s*(\d+)\s*[\]】］]", answer)}
    by_index = {citation.get("index"): citation for citation in citations}
    if not referenced or not referenced.issubset(by_index):
        issues.append("missing_or_unknown_citation_index")
    for citation in citations:
        key = ids.get(citation.get("document_id"))
        page = citation.get("page")
        if key is None or not isinstance(page, int) or not 1 <= page <= len(fixtures[key]["pages"]):
            issues.append("citation_source_does_not_exist")
    for support in item["support"]:
        expected_id = documents[support["document"]]["id"]
        source = fixtures[support["document"]]["pages"][support["page"] - 1]
        matches = [by_index[index] for index in referenced if index in by_index
                   and by_index[index].get("document_id") == expected_id
                   and by_index[index].get("page") == support["page"]]
        if not any(all(known_value_in_text(citation.get("quote", ""), token)
                       and known_value_in_text(source, token) for token in support["tokens"])
                   for citation in matches):
            issues.append("required_fact_not_supported_by_cited_page")
    return {"passed": not issues, "safe_nonfactual_refusal": False, "issues": sorted(set(issues)), **scope}


def p95(values: list[float]) -> float:
    if not values:
        raise ValueError("No measured model calls")
    return sorted(values)[math.ceil(len(values) * .95) - 1]


def assess_backends(response: dict[str, Any], models: dict[str, str]) -> bool:
    metrics = response.get("retrieval_metrics") or {}
    embedding, reranker = metrics.get("embedding") or {}, metrics.get("reranker") or {}
    return (embedding.get("backend") == "omlx" and not embedding.get("degraded")
            and str(embedding.get("model", "")).startswith(models["embedding"] + "@")
            and reranker.get("enabled") is True and reranker.get("model") == models["reranker"]
            and (not response.get("llm_used") or response.get("provenance", {}).get("model") == models["llm"]))


class ChatClient:
    def __init__(self, base: str, token: str, role: str):
        self.base, self.token, self.role = base, token, role

    def call(self, path: str, payload: Any = None) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Authorization": "Bearer " + self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers,
                                         method="POST" if data is not None else "GET")
        started = time.perf_counter()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=180) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except OSError as exc:
            return {"status": 0, "seconds": time.perf_counter() - started, "error_type": type(exc).__name__,
                    "response": None, "role": self.role}
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"unparsed_response": raw.decode("utf-8", errors="replace")}
        return {"status": status, "seconds": time.perf_counter() - started, "response": body, "role": self.role}


def run(base: str, token: str, protocol_path: Path, output: Path) -> dict[str, Any]:
    base = local_url(base)
    require(not output.exists(), "Acceptance output must be a new directory")
    protocol_bytes = protocol_path.read_bytes()
    protocol = json.loads(protocol_bytes)
    require(protocol.get("format") == "lexvault-local-model-acceptance-v1", "Unexpected frozen protocol")
    output.mkdir(parents=True, mode=0o700)
    journal: list[dict[str, Any]] = []
    admin = Session(base, "admin", journal)
    admin.login(token)
    actual_model = admin.call("GET", "/api/model-config")
    require(actual_model["model"] == protocol["model_ids"]["llm"], "Configured model differs from protocol")
    case_id = admin.call("POST", "/api/cases", {"title": "真实模型合成验收-" + secrets.token_hex(4)}, 201)["id"]
    clients = {"admin": ChatClient(base, token, "admin")}
    for role, permissions in ROLES.items():
        member = admin.call("POST", "/api/members", {"name": "模型验收-" + role + "-" + secrets.token_hex(3),
                            "permissions": permissions}, 201)
        admin.call("PUT", f"/api/cases/{case_id}/members/{member['member']['id']}", {"role": role})
        clients[role] = ChatClient(base, member["token"], role)
    documents = {}
    for key, fixture in protocol["fixtures"].items():
        document = admin.upload(case_id, fixture["name"], "\f".join(fixture["pages"]))
        documents[key] = {"id": document["id"], "pages": document["pages"]}
        for page, expected_text in enumerate(fixture["pages"], 1):
            actual = admin.call("GET", f"/api/documents/{document['id']}/pages/{page}")
            require(normalize(actual["text"]) == normalize(expected_text), "Uploaded fixture page differs from protocol")
    state = {"case_id": case_id, "documents": documents, "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
             "configured_model": actual_model, "synthetic_only": True}
    (output / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    (output / "setup-journal.json").write_text(json.dumps(journal, ensure_ascii=False, indent=2) + "\n")
    stop = threading.Event()
    readings: list[dict[str, Any]] = []

    def reader(role: str) -> None:
        while not stop.is_set():
            item = clients[role].call(f"/api/cases/{case_id}")
            readings.append({key: value for key, value in item.items() if key != "response"})
            stop.wait(.25)

    def question(index: int, item: dict[str, Any]) -> dict[str, Any]:
        role = ("admin", "owner", "assistant")[index % 3]
        sample = clients[role].call(f"/api/cases/{case_id}/chat", {"question": item["question"], "use_llm": True,
                                   "use_remote_embeddings": True, "request_id": "model-acceptance-" + secrets.token_hex(12)})
        grade: dict[str, Any] = assess_answer(item, sample["response"], documents, protocol["fixtures"]) if sample["status"] == 200 else {
            "passed": False, "issues": ["unexpected_http_failure"]}
        if sample["status"] == 200 and not assess_backends(sample["response"], protocol["model_ids"]):
            grade["passed"] = False
            grade["issues"].append("actual_model_backend_mismatch_or_degradation")
        result = {"id": item["id"], "kind": item["kind"], "question": item["question"], **sample, "assessment": grade}
        (output / (item["id"] + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result

    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        readers = [executor.submit(reader, role) for role in ("reviewer", "viewer")]
        futures = [executor.submit(question, index, item) for index, item in enumerate(protocol["questions"])]
        try:
            results = [future.result() for future in futures]
        finally:
            stop.set()
            for future in readers:
                future.result()
    llm_samples = [item["seconds"] for item in results if item["status"] == 200 and item["response"].get("llm_used")]
    failures = [item["id"] for item in results if not item["assessment"]["passed"]]
    latency = p95(llm_samples) if llm_samples else None
    passed = (not failures and len(llm_samples) >= protocol["thresholds"]["minimum_actual_llm_answers"]
              and latency is not None and latency <= protocol["thresholds"]["llm_p95_seconds"]
              and bool(readings) and all(item["status"] == 200 for item in readings))
    summary = {"stage": "passed" if passed else "failed", "case_id": case_id, "synthetic_only": True,
               "protocol_sha256": state["protocol_sha256"], "questions": len(results), "actual_llm_answers": len(llm_samples),
               "passed_questions": len(results) - len(failures), "failed_questions": failures,
               "llm_p95_seconds": latency, "concurrent_reads": len(readings),
               "read_failures": sum(item["status"] != 200 for item in readings), "manual_answer_review": "pending",
               "automatic_scope": "frozen known-value tokens and cited-page support; not semantic/legal entailment",
               "semantic_entailment_checked": False}
    (output / "read-journal.json").write_text(json.dumps(readings, indent=2) + "\n")
    (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--admin-token-env", default="LAW_REVIEW_ACCEPTANCE_ADMIN_TOKEN")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--synthetic-instance", action="store_true", required=True)
    args = parser.parse_args(argv)
    token = os.getenv(args.admin_token_env, "")
    require(bool(token), "Administrator token environment variable is empty")
    result = run(args.base_url, token, args.protocol, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["stage"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
