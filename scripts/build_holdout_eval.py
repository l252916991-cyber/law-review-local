"""Build/check the frozen six-case holdout set from the authored synthetic fixture.

The holdout cases are independent from the development case: different parties,
amounts, document names and questions. Nothing here may be tuned against
development results; the freeze exists so later runs can be recomputed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DIRECTORY = ROOT / "benchmarks/answer_eval/holdout-v1"
CATEGORIES = {"normal", "contradiction", "insufficient", "irrelevant", "statute_missing", "injection"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def load_cases(path: Path = DIRECTORY / "cases.json") -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format_version") != 1:
        raise ValueError("fixture 版本不受支持")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != 6:
        raise ValueError("保留集必须包含 6 个独立案例")
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for case in cases:
        case_id = case.get("case_id")
        if type(case_id) is not int or case_id < 1 or case_id in seen_ids:
            raise ValueError(f"case_id 必须为正整数且唯一：{case_id}")
        seen_ids.add(case_id)
        if not isinstance(case.get("description"), str) or not case["description"].strip():
            raise ValueError(f"案例缺少 description：{case_id}")
        documents = case.get("documents")
        if not isinstance(documents, list) or not documents:
            raise ValueError(f"案例缺少 documents：{case_id}")
        for document in documents:
            name = document.get("name")
            if not isinstance(name, str) or not name or name in seen_names:
                raise ValueError(f"文档名必须非空且跨案例唯一：{name}")
            seen_names.add(name)
            pages = document.get("pages")
            if not isinstance(pages, list) or not pages or any(not isinstance(t, str) or not t.strip() for t in pages):
                raise ValueError(f"文档页码正文必须非空：{name}")
        questions = case.get("questions")
        if not isinstance(questions, list) or len(questions) != 4:
            raise ValueError(f"每案必须有 4 题：{case_id}")
    return payload


def build(fixture: dict[str, Any]) -> dict[str, Any]:
    from app.evaluation.answer_eval import validate_dataset

    sources: list[dict[str, Any]] = []
    for case in fixture["cases"]:
        for document in case["documents"]:
            for page_no, text in enumerate(document["pages"], 1):
                sources.append({
                    "case_id": case["case_id"], "document_name": document["name"], "page": page_no,
                    "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                })
    items: list[dict[str, Any]] = []
    for case in fixture["cases"]:
        for question in case["questions"]:
            if question.get("category") not in CATEGORIES:
                raise ValueError(f"类别无效：{question.get('id')}")
            items.append({
                **question,
                "case_id": case["case_id"],
                "case_description": case["description"],
                "requires_citation": bool(question.get("requires_citation", not question["expect_refusal"])),
            })
    payload = {
        "name": fixture["name"], "version": 2, "case_fixture": "authored-cases-holdout-v1",
        "annotation_source": "assistant_generated_from_authored_synthetic_fixture",
        "notice": fixture["notice"], "limitations": fixture["limitations"],
        "source_sha256": hashlib.sha256(canonical(sources)).hexdigest(),
        "sources": sources,
        "cases": [
            {key: case[key] for key in ("case_id", "title", "case_no", "case_type", "client_name", "description", "documents")}
            for case in fixture["cases"]
        ],
        "items": items,
    }
    validate_dataset(payload)
    validate_cross_case(payload)
    return payload


def validate_cross_case(payload: dict[str, Any]) -> None:
    """A question may only cite pages of its own case; that is what makes the cases independent."""
    owner = {(source["document_name"], source["page"]): source["case_id"] for source in payload["sources"]}
    for item in payload["items"]:
        for name, page in item["acceptable_pages"]:
            if owner.get((name, page)) != item["case_id"]:
                raise ValueError(f"{item['id']} 引用了其他案例或不存在的页面：{name} 第{page}页")
    if len({item["case_id"] for item in payload["items"]}) != len(payload["cases"]):
        raise ValueError("每个案例都必须有题目")


def manifest_for(dataset: dict[str, Any], encoded: bytes, cases_bytes: bytes) -> dict[str, Any]:
    return {
        "format_version": 2,
        "dataset_sha256": hashlib.sha256(encoded).hexdigest(),
        "cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "source_sha256": dataset["source_sha256"],
        "items": len(dataset["items"]),
        "cases": len(dataset["cases"]),
        "pages": len(dataset["sources"]),
        "categories": {key: sum(item["category"] == key for item in dataset["items"])
                       for key in sorted({item["category"] for item in dataset["items"]})},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    parser.add_argument("--write", action="store_true", help="只写新的或逐字节相同的冻结产物")
    args = parser.parse_args(argv)
    cases_path = args.directory / "cases.json"
    cases_bytes = cases_path.read_bytes()
    dataset = build(load_cases(cases_path))
    encoded = (json.dumps(dataset, ensure_ascii=False, indent=2) + "\n").encode()
    manifest = manifest_for(dataset, encoded, cases_bytes)
    outputs = {"dataset.json": encoded,
               "manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()}
    for name, content in outputs.items():
        path = args.directory / name
        if path.exists() and path.read_bytes() != content:
            raise ValueError(f"冻结产物不同：{name}；请建立新版本，不改写既有冻结集")
    for name, content in outputs.items():
        path = args.directory / name
        if not path.exists():
            if not args.write:
                raise ValueError(f"缺少冻结产物：{name}")
            with path.open("xb") as stream:
                stream.write(content)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
