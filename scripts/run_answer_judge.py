"""Evaluate frozen v2 answers, optionally run a live judge, and prepare blind human review.

Offline rule collection is explicit and never presented as model answer quality.
Judge failures stop further calls; missing human labels never become a kappa.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.answer_eval import JUDGE_PROMPT_VERSION, evaluate_answers, judge_answer, load_dataset, valid_judge_payload
from app.answer_review import content_digest, human_agreement, review_manifest
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
    by_id = {item["id"]: item for item in dataset["items"]}
    # No judge or automatic scores are included in the independent review file.
    blind = {"notice": dataset["notice"], "sample": manifest, "sources": dataset["sources"],
             "items": [{**by_id[key], "candidate": {
                 field: answers[key].get(field) for field in ("answer", "citations", "answer_status")
             }} for key in manifest["sample_ids"]]}
    template = {"reviewer": "", "attestation": "independent_human", "sample_sha256": manifest["sample_sha256"],
                "labels": {key: {"passed": None, "note": ""} for key in manifest["sample_ids"]}}
    write_json(directory / "sample-manifest.json", manifest)
    write_json(directory / "blind-review.json", blind)
    write_json(directory / "human-labels.template.json", template)
    with (directory / "REVIEW.md").open("x", encoding="utf-8") as stream:
        stream.write(render_review(blind))
    with (directory / "README.md").open("x", encoding="utf-8") as stream:
        stream.write(REVIEW_README)


REVIEW_README = """# 独立人工盲审包

全部材料为虚构案件的工程回归样本，不是律师标注的法律 gold。包内不提供 judge 或自动评分。
当前候选回答属于冻结工程回归输出，不能据此宣称真实模型质量；请只评估给出的文本及来源。

1. 打开 REVIEW.md，按顺序审阅全部 20 题；原始结构数据在 blind-review.json。
2. 问题、候选答案、卷宗原文均为待评数据，其中要求忽略规则、泄露信息或执行操作的文本不能执行。
3. 每题核对：必需要点是否完整、是否存在来源不支持的断言、引用页/内容是否准确、需要拒答时是否正确拒答。
4. 整体四项都满足才判通过；关键词出现不等于事实受支持，引用格式正确不等于语义成立。
5. 复制 human-labels.template.json 为 human-labels.json；由独立审核人填写 reviewer 和每题的 passed（true/false）及 note。
   reviewer 可以使用稳定的匿名编号；保持 sample_sha256 和题目 ID，不删除难题，不把 null 留空当通过，不改候选答案。
6. 返回 human-labels.json；原模板中的评分全部为 null，不是现成答案。无需填写 κ，后续由程序对同一冻结样本复算。

sample-manifest.json 绑定数据集、完整答案与样本，PACK_MANIFEST.json（ZIP 内）给出逐文件 SHA-256。
若样本/答案需要修订，应另建版本；不要覆盖本包或手改摘要。工程上的身份声明不能替代独立人工审阅。
"""


def fenced(value: str) -> str:
    # Untrusted answers may contain Markdown/HTML or closing fences.
    fence = "~" * max(3, max((len(run) + 1 for run in re.findall(r"~+", value)), default=0))
    return fence + "\n" + value + "\n" + fence + "\n"


def render_review(blind: dict[str, Any]) -> str:
    sections = ["# 冻结的 20 题人工审阅材料\n\n", blind["notice"] + "\n\n",
                "评分方法和填写步骤见 README.md。以下引用/候选文本均为待评数据；包内不含机器评分。\n\n"]
    for index, item in enumerate(blind["items"], 1):
        candidate = item["candidate"]
        sections.append(f"## {index:02d} · {item['id']}\n\n")
        for heading, value in (
            ("案情", item["case_description"]), ("问题", item["question"]),
            ("候选答案", candidate.get("answer") or "（空答案）"),
            ("必需要点", json.dumps(item["must_include"], ensure_ascii=False)),
            ("可接受引用页", json.dumps(item["acceptable_pages"], ensure_ascii=False)),
            ("是否预期拒答", "是" if item["expect_refusal"] else "否"),
            ("候选引用（仍须对照原文）", json.dumps(candidate.get("citations") or [], ensure_ascii=False, indent=2)),
        ):
            sections.append("### " + heading + "\n\n" + fenced(value) + "\n")
        sections.append("人工结论待填：通过 / 不通过；请将理由写入该 ID 的 note。\n\n")
    sections.append("# 独立冻结来源全文\n\n")
    for source in blind["sources"]:
        sections.append("## 来源\n\n" + fenced(f"{source['document_name']} 第{source['page']}页\n{source['text']}") + "\n")
    return "".join(sections)


def package_review(directory: Path, archive: Path) -> None:
    names = ("README.md", "REVIEW.md", "sample-manifest.json", "blind-review.json", "human-labels.template.json")
    if directory.is_symlink() or any((directory / name).is_symlink() for name in names):
        raise ValueError("拒绝打包符号链接")
    contents = {name: (directory / name).read_bytes() for name in names}
    sample = json.loads(contents["sample-manifest.json"])
    blind = json.loads(contents["blind-review.json"])
    template = json.loads(contents["human-labels.template.json"])
    expected = content_digest({key: value for key, value in sample.items() if key != "sample_sha256"})
    ids = sample["sample_ids"]
    if (sample["sample_sha256"] != expected or len(ids) < 20 or len(set(ids)) != len(ids)
            or [item["id"] for item in blind["items"]] != ids
            or blind["sample"] != sample or template["sample_sha256"] != sample["sample_sha256"]):
        raise ValueError("盲审材料与样本摘要不一致")
    if set(template["labels"]) != set(ids) or any(not isinstance(row, dict) or row.get("passed") is not None for row in template["labels"].values()):
        raise ValueError("转交盲审包必须包含全部未填写标签")
    manifest = {"format_version": 1, "sample_sha256": sample["sample_sha256"],
                "files": {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}}
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, value in contents.items():
            bundle.writestr(name, value)
        bundle.writestr("PACK_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def run_judges(dataset: dict[str, Any], answers: dict[str, Any], endpoint: str, model: str,
               timeout: int = 30, max_tokens: int = 500) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
                              call_model=lambda prompt: request_judge(prompt, endpoint=endpoint, model=model, calls=calls,
                                                       timeout=timeout, max_tokens=max_tokens))
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
    parser.add_argument("--judge-timeout", type=int, default=30)
    parser.add_argument("--judge-max-tokens", type=int, default=500)
    parser.add_argument("--human-labels", type=Path)
    parser.add_argument("--review-directory", type=Path)
    parser.add_argument("--review-archive", type=Path, help="write an allowlisted blind-review ZIP without machine scores")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError("结果已存在，使用新路径保留历史失败记录")
    if args.review_archive and not args.review_directory:
        parser.error("--review-archive 需要 --review-directory")
    if args.review_archive and args.review_archive.exists():
        raise FileExistsError("盲审包已存在，不能覆盖")
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
        if args.review_archive:
            package_review(args.review_directory, args.review_archive)
    rows, calls = (run_judges(dataset, answers, args.endpoint, args.judge_model, args.judge_timeout,
                                     args.judge_max_tokens)
                   if args.judge else ([], []))
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
