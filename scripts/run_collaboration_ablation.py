"""Prepare, precheck and run the isolated A/B/C specialist-handoff ablation.

``prepare`` freezes retrieval contexts, rule-specialist findings and all three
prompts without calling a model. ``precheck`` probes the configured endpoint and
stops before any batch call when it is unavailable. ``run`` consumes a frozen
batch and drives exactly one generation per planned attempt.

The rules are explicit: no group is added or removed at runtime, a failed
attempt is never dropped from a denominator, an unknown token count is never
written as zero, and an unavailable model ends the run instead of producing a
partial batch that looks complete.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.collaboration_ablation import (  # noqa: E402
    GROUPS, MAX_PROMPT_CHARS, live_generator, load_batch_dataset, load_prepared,
    precheck, prepare_batch, run_batch,
)

EXIT_PROTOCOL = 2
EXIT_MODEL_UNAVAILABLE = 3


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def parse_groups(value: str) -> list[str]:
    groups = [part.strip().upper() for part in value.split(",") if part.strip()]
    unknown = [group for group in groups if group not in GROUPS]
    if not groups or unknown:
        raise argparse.ArgumentTypeError(f"分组必须是 {','.join(GROUPS)} 的非空子集，收到 {value!r}")
    return groups


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三组隔离对照：准备、预检、执行")
    parser.add_argument("--mode", choices=("prepare", "precheck", "run"), required=True)
    parser.add_argument("--dataset", type=Path, default=ROOT / "benchmarks/answer_eval/v2/dataset.json")
    parser.add_argument("--output", type=Path, required=True, help="批次目录；run 模式读取该冻结批次")
    parser.add_argument("--run-output", type=Path, help="run 模式的产物目录，必须不存在，默认 <output>/run")
    parser.add_argument("--precheck-output", type=Path, help="precheck 记录路径，默认 <output>/precheck.json")
    parser.add_argument("--groups", type=parse_groups, default=list(GROUPS))
    parser.add_argument("--item-ids", help="逗号分隔的题目 id；省略表示全量")
    parser.add_argument("--limit", type=int, default=6, help="每题冻结检索页数")
    parser.add_argument("--repeats", type=int, default=1, help="同一题目的重复轮次（保留集用 3）")
    parser.add_argument("--endpoint", help="显式模型端点；默认读取当前配置")
    parser.add_argument("--model", help="显式模型 ID；默认读取当前配置")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-prompt-chars", type=int, default=MAX_PROMPT_CHARS)
    parser.add_argument("--max-output-tokens", type=int, default=900)
    args = parser.parse_args(argv)

    if args.repeats < 1:
        parser.error("--repeats 必须 >= 1")
    if args.limit < 1:
        parser.error("--limit 必须 >= 1")

    dataset = load_batch_dataset(args.dataset)

    if args.mode == "prepare":
        item_ids = [part.strip() for part in args.item_ids.split(",") if part.strip()] if args.item_ids else None
        try:
            manifest = prepare_batch(dataset, args.output, item_ids=item_ids, limit=args.limit)
        except (FileExistsError, ValueError) as exc:
            print(f"准备失败：{exc}", file=sys.stderr)
            return EXIT_PROTOCOL
        print(json.dumps({
            "batch": str(args.output.absolute()), "items": len(manifest["items"]),
            "prompt_chars": {
                group: {
                    "min": min(entry["prompt_chars"][group] for entry in manifest["items"]),
                    "max": max(entry["prompt_chars"][group] for entry in manifest["items"]),
                } for group in GROUPS
            },
            "dataset": manifest["dataset"], "case": manifest["case"],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.mode == "precheck":
        result = precheck(args.endpoint, args.model)
        target = args.precheck_output or (args.output / "precheck.json")
        try:
            write_json(target, result)
        except FileExistsError:
            print(f"预检记录已存在：{target}；换新路径或新批次身份，不覆盖旧记录", file=sys.stderr)
            return EXIT_PROTOCOL
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["available"] else EXIT_MODEL_UNAVAILABLE

    if not args.output.exists():
        print("run 模式需要已冻结的批次目录；先执行 --mode prepare", file=sys.stderr)
        return EXIT_PROTOCOL
    manifest, prepared = load_prepared(args.output)
    run_output = args.run_output or (args.output / "run")
    if run_output.exists():
        print(f"{run_output} 已存在；修复后用新批次身份重跑，不覆盖旧尝试记录", file=sys.stderr)
        return EXIT_PROTOCOL
    probe = precheck(args.endpoint, args.model)
    if not probe["available"]:
        run_output.mkdir(parents=True, mode=0o700)
        write_json(run_output / "precheck.json", probe)
        write_json(run_output / "failure.json", {
            "outcome": "model_unavailable", "precheck": probe, "batch_calls": 0,
            "batch": str(args.output.absolute()), "attempts_not_started": True,
        })
        print(f"模型端点不可用：{probe['endpoint']} / {probe['model']}；未发起批量调用", file=sys.stderr)
        return EXIT_MODEL_UNAVAILABLE
    if probe["max_tokens"] != args.max_output_tokens:
        run_output.mkdir(parents=True, mode=0o700)
        write_json(run_output / "precheck.json", probe)
        write_json(run_output / "failure.json", {
            "outcome": "budget_mismatch", "precheck": probe, "batch_calls": 0,
            "registered_max_output_tokens": args.max_output_tokens,
            "configured_max_tokens": probe["max_tokens"], "attempts_not_started": True,
        })
        print("登记的 max_output_tokens 与实际配置不符；未发起批量调用", file=sys.stderr)
        return EXIT_PROTOCOL
    resolved_model = str(probe["model"])
    report = run_batch(
        dataset, prepared, run_output,
        generate=live_generator(resolved_model, args.timeout, probe["endpoint"]),
        model=resolved_model, groups=args.groups, repeats=args.repeats,
        max_prompt_chars=args.max_prompt_chars, max_output_tokens=args.max_output_tokens,
        timeout=args.timeout, precheck_payload=probe,
    )
    print(json.dumps({"run": str(run_output.absolute()), "items": len(manifest["items"]),
                      "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
