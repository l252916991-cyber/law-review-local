"""Score answer-level evaluation; offline by default, live collection optional.

Offline (CI / no model)::

    uv run --locked python scripts/run_answer_eval.py --answers answers.json

The answers file maps item id to ``{"answer": str, "citations": [...],
"answer_status": str}``. Collecting those answers needs a model and is a
separate step; this script never invents one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.answer_eval import (  # noqa: E402
    cohen_kappa,
    evaluate_answers,
    judge_answer,
    load_dataset,
)


def collect_live(case_id: int, dataset: dict) -> dict:
    from app.services import chat

    answers = {}
    for item in dataset["items"]:
        result = chat(case_id, item["question"], "本机律师", None, True, True)
        answers[item["id"]] = {
            "answer": result["answer"],
            "citations": result["citations"],
            "answer_status": result.get("answer_status"),
        }
    return answers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="答案级评测")
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/answer_eval/v1/demo-case.json"))
    parser.add_argument("--answers", type=Path, help="已收集的 answers.json（离线评分）")
    parser.add_argument("--case-id", type=int, help="在线收集答案时使用的案件 id")
    parser.add_argument("--live", action="store_true", help="调用本地模型逐题作答后再评分")
    parser.add_argument("--human-labels", type=Path, help="人工通过/不通过标签 {id: bool}")
    parser.add_argument("--output", type=Path, help="把结果写入该 JSON 文件")
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset)
    if args.live:
        if not args.case_id:
            parser.error("--live 需要 --case-id")
        answers = collect_live(args.case_id, dataset)
    elif args.answers:
        answers = json.loads(args.answers.read_text(encoding="utf-8"))
    else:
        parser.error("需要 --answers（离线评分）或 --live --case-id（在线收集）")

    report = evaluate_answers(dataset, answers)

    if args.human_labels:
        labels = json.loads(args.human_labels.read_text(encoding="utf-8"))
        if not isinstance(labels, dict) or any(type(value) is not bool for value in labels.values()):
            raise ValueError("人工标签必须是 {id: bool}，不能转换字符串或空值")
        ids = [row["id"] for row in report["per_item"] if row["id"] in labels]
        if not ids:
            raise ValueError("人工标签与数据集没有共同题目")
        if ids:
            auto = [row["passed"] for row in report["per_item"] if row["id"] in labels]
            human = [bool(labels[item_id]) for item_id in ids]
            report["human_agreement"] = {
                "compared_labels": "deterministic_rules_not_judge",
                "items": len(ids),
                "kappa": cohen_kappa(auto, human),
                "disagreements": [item_id for item_id, a, h in zip(ids, auto, human) if a != h],
            }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        summary = report["summary"]
        print(
            f"{dataset['name']}: {summary['passed']}/{summary['queries']} 通过"
            f" · 要点覆盖 {summary['coverage_avg']} · 引用精度 {summary['citation_precision_avg']}"
            f" · 无据断言 {summary['violation_count']}"
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
