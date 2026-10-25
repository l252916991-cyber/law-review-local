"""Evaluate frozen v2 answers, optionally run a live judge, and prepare blind human review.

Offline rule collection is explicit and never presented as model answer quality.
Judge failures stop further calls; missing human labels never become a kappa.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.answer_eval import JUDGE_PROMPT_VERSION, evaluate_answers, judge_answer, load_dataset, valid_judge_payload
from app.answer_review import human_agreement, review_manifest
from scripts.validate_application_acceptance import isolated_environment, loopback_endpoint


def read_json(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("输入必须是 JSON 对象")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def collect_rules(dataset: dict[str, Any]) -> dict[str, Any]:
    from app.db import db_scope, init_db
    from app.services import chat

    with tempfile.TemporaryDirectory(prefix="lexvault-rule-answers-") as td:
        directory = Path(td)
        with isolated_environment(directory, "http://127.0.0.1:1/v1", "no-model", ""):
            with db_scope(directory / "law_review.db"):
                init_db(seed=True)
                answers = {}
                for item in dataset["items"]:
                    result = chat(1, item["question"], "合成离线回归", None, False, False)
                    answers[item["id"]] = {key: result.get(key) for key in ("answer", "citations", "answer_status")}
                return answers


def prepare_review(directory: Path, dataset: dict[str, Any], answers: dict[str, Any], manifest: dict[str, Any]) -> None:
    selected = set(manifest["sample_ids"])
    # No judge or automatic scores are included in the independent review file.
    blind = {"notice": dataset["notice"], "sample": manifest, "sources": dataset["sources"],
             "items": [{**item, "candidate": answers[item["id"]]} for item in dataset["items"] if item["id"] in selected]}
    template = {"reviewer": "", "attestation": "independent_human", "sample_sha256": manifest["sample_sha256"],
                "labels": {key: {"passed": None, "note": ""} for key in manifest["sample_ids"]}}
    write_json(directory / "sample-manifest.json", manifest)
    write_json(directory / "blind-review.json", blind)
    write_json(directory / "human-labels.template.json", template)


def run_judges(dataset: dict[str, Any], answers: dict[str, Any], endpoint: str, model: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from app.answer_judge_client import request_judge

    rows: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    stopped = False
    for item in dataset["items"]:
        if stopped:
            rows.append({"id": item["id"], "available": False, "passed": None,
                         "semantic_entailment_checked": False, "error": "not_attempted_after_judge_failure"})
            continue
        entry = answers[item["id"]]
        result = judge_answer(item, entry.get("answer", ""), entry.get("citations", []),
                              source_pages=dataset["sources"],
                              call_model=lambda prompt: request_judge(prompt, endpoint=endpoint, model=model, calls=calls))
        rows.append(result)
        stopped = not result["available"]
    return rows, calls


def cached_judges(path: Path, dataset: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    report = read_json(path)
    if report.get("sample") != manifest or report.get("judge_version") != JUDGE_PROMPT_VERSION:
        raise ValueError("judge 缓存的来源、答案、样本或提示版本不匹配")
    rows = report.get("judge_per_item", [])
    if not isinstance(rows, list) or len(rows) != len(dataset["items"]):
        raise ValueError("judge 缓存必须保留全部逐题结果")
    by_id = {row["id"]: row for row in rows}
    if set(by_id) != {item["id"] for item in dataset["items"]}:
        raise ValueError("judge 缓存题目不匹配")
    for item in dataset["items"]:
        row = by_id[item["id"]]
        valid = (row.get("available") is True and row.get("source_basis") == "frozen_dataset"
                 and row.get("semantic_entailment_checked") is True and valid_judge_payload(row.get("parsed")))
        row["semantic_entailment_checked"] = valid
        parsed = row.get("parsed") or {}
        row["passed"] = (bool(parsed["coverage_ok"] and parsed["citation_ok"] and not parsed["unsupported_claims"]
                              and (not item["expect_refusal"] or parsed["refusal_ok"])) if valid else None)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "benchmarks/answer_eval/v2/dataset.json")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--answers", type=Path)
    source.add_argument("--collect-rules", action="store_true")
    parser.add_argument("--answers-output", type=Path)
    judge = parser.add_mutually_exclusive_group()
    judge.add_argument("--judge", action="store_true", help="explicitly call a loopback model; fail fast on any judge error")
    judge.add_argument("--judge-results", type=Path, help="recompute agreement offline from a digest-bound prior report")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--judge-model", default="Qwythos-9B-v2-OptiQ-4bit")
    parser.add_argument("--human-labels", type=Path)
    parser.add_argument("--review-directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError("结果已存在，使用新路径保留历史失败记录")
    dataset = load_dataset(args.dataset)
    if dataset.get("version") != 2:
        raise ValueError("此闭环只接受带独立来源冻结的 v2 数据集")
    endpoint = loopback_endpoint(args.endpoint)
    envelope = read_json(args.answers) if args.answers else {}
    answers = collect_rules(dataset) if args.collect_rules else envelope.get("answers", envelope)
    mode = "rules_no_model" if args.collect_rules else envelope.get("collection_mode", "user_supplied_unverified")
    report = evaluate_answers(dataset, answers)
    manifest = review_manifest(dataset, answers)
    human = read_json(args.human_labels) if args.human_labels else None
    # Validate human provenance/types before any model request.
    if human is not None:
        human_agreement(manifest, [], human)
    if args.answers_output:
        write_json(args.answers_output, {"collection_mode": mode, "answers": answers})
    if args.review_directory:
        prepare_review(args.review_directory, dataset, answers, manifest)
    rows, calls = run_judges(dataset, answers, endpoint, args.judge_model) if args.judge else ([], [])
    if args.judge_results:
        rows = cached_judges(args.judge_results, dataset, manifest)
    agreement = human_agreement(manifest, rows, human)
    judge_complete = bool(rows) and all(row.get("semantic_entailment_checked") is True for row in rows)
    prior = read_json(args.judge_results) if args.judge_results else {}
    report.update(collection_mode=mode, sample=manifest, judge_version=JUDGE_PROMPT_VERSION,
                  judge_model=args.judge_model if args.judge else prior.get("judge_model"), judge_per_item=rows, judge_calls=calls,
                  reused_judge_sample_sha256=prior.get("sample", {}).get("sample_sha256"),
                  judge_status="completed" if judge_complete else "failed" if args.judge or args.judge_results else "not_requested",
                  judge_execution="live" if args.judge else "cached" if args.judge_results else "not_requested",
                  human_agreement=agreement, acceptance_complete=judge_complete and agreement["status"] == "computed")
    write_json(args.output, report)
    print(json.dumps({"queries": report["summary"]["queries"], "rule_passed": report["summary"]["passed"],
                      "judge_status": report["judge_status"], "human_status": agreement["status"],
                      "acceptance_complete": report["acceptance_complete"]}, ensure_ascii=False))
    return 0 if not (args.judge or args.judge_results) or report["acceptance_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
