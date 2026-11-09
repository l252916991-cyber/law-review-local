"""Build/check an immutable v2 answer dataset from the repository synthetic seed."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def build(questions: list[dict[str, Any]]) -> dict[str, Any]:
    from app.evaluation.answer_eval import validate_dataset
    from app.storage.db import connect, db_scope, init_db

    with tempfile.TemporaryDirectory(prefix="lexvault-answer-sources-") as td:
        with db_scope(Path(td) / "law_review.db"):
            init_db(seed=True)
            with connect() as conn:
                sources = [{"document_name": row["name"], "page": row["page_no"], "text": row["text"],
                            "sha256": hashlib.sha256(row["text"].encode()).hexdigest()}
                           for row in conn.execute("SELECT d.name,p.page_no,p.text FROM pages p JOIN documents d "
                                                   "ON d.id=p.document_id WHERE d.case_id=1 ORDER BY d.name,p.page_no")]
    description = "完全虚构的某科技公司稳盈计划侦查材料；含询问笔录、账户流水和宣传邮件，尚无判决文书或已核验法条。"
    items = [{**item, "case_description": description,
              "requires_citation": not item["expect_refusal"]} for item in questions]
    # "case_fixture" is a frozen label inside the hashed dataset; the fixture now lives in app.storage.db.
    payload = {"name": "demo-case-answer-v2", "version": 2, "case_fixture": "app.db.seed_demo",
               "annotation_source": "assistant_generated_from_repository_synthetic_fixture",
               "notice": "编码助手依仓库合成卷宗构造，未经律师或独立人工标注。仅用于答案级回归，不代表法律准确率。",
               "limitations": ["单个虚构案件", "关键词规则可能受同义表达与否定句影响", "与页级检索指标分开报告"],
               "source_sha256": hashlib.sha256(canonical(sources)).hexdigest(), "sources": sources, "items": items}
    validate_dataset(payload)
    if not 50 <= len(items) <= 100:
        raise ValueError("冻结集必须含 50–100 题")
    if {item["category"] for item in items} != {"normal", "contradiction", "insufficient", "irrelevant", "statute_missing", "injection"}:
        raise ValueError("六类场景必须全部覆盖")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "benchmarks/answer_eval/v2")
    parser.add_argument("--write", action="store_true", help="write only new or byte-identical frozen outputs")
    args = parser.parse_args(argv)
    questions = json.loads((args.directory / "questions.json").read_text(encoding="utf-8"))
    dataset = build(questions)
    encoded = (json.dumps(dataset, ensure_ascii=False, indent=2) + "\n").encode()
    manifest = {"format_version": 1, "dataset_sha256": hashlib.sha256(encoded).hexdigest(),
                "questions_sha256": hashlib.sha256((args.directory / "questions.json").read_bytes()).hexdigest(),
                "source_sha256": dataset["source_sha256"], "items": len(dataset["items"]),
                "categories": {key: sum(item["category"] == key for item in dataset["items"])
                               for key in sorted({item["category"] for item in dataset["items"]})}}
    outputs = {"dataset.json": encoded, "manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()}
    for name, content in outputs.items():
        path = args.directory / name
        if path.exists() and path.read_bytes() != content:
            raise ValueError(f"冻结产物不同: {name}；请建立新版本，不改写既有冻结集")
        if not path.exists():
            if not args.write:
                raise ValueError(f"缺少冻结产物: {name}")
    for name, content in outputs.items():
        path = args.directory / name
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(content)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
