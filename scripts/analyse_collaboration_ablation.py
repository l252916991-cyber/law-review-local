"""Reduce an A/B/C ledger to a paired report; reads recorded attempts only."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ablation_analysis import GROUPS, analyse, load_attempts  # noqa: E402

EXIT_PROTOCOL = 2


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:  # never overwrite a previous report
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三组账本配对分析")
    parser.add_argument("--run", type=Path, required=True, help="含 attempts.jsonl 的批次目录")
    parser.add_argument("--attempts", type=Path, help="直接指定 attempts.jsonl")
    parser.add_argument("--baseline", choices=GROUPS, default="A")
    parser.add_argument("--compare", choices=GROUPS, default="C")
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261027)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    attempts = args.attempts or (args.run / "attempts.jsonl")
    if not attempts.is_file():
        print(f"找不到账本：{attempts}", file=sys.stderr)
        return EXIT_PROTOCOL
    if args.iterations < 100:
        parser.error("--iterations 至少 100")
    if args.baseline == args.compare:
        parser.error("--baseline 与 --compare 不能相同")
    try:
        report = analyse(load_attempts(attempts), args.baseline, args.compare,
                         iterations=args.iterations, seed=args.seed)
    except ValueError as exc:
        print(f"分析失败：{exc}", file=sys.stderr)
        return EXIT_PROTOCOL
    report |= {"source": str(attempts), "run": str(args.run) if args.run else None}
    try:
        write_json(args.output, report)
    except FileExistsError:
        print(f"{args.output} 已存在；换新路径，不覆盖旧报告", file=sys.stderr)
        return EXIT_PROTOCOL
    summary = report["summary"]
    print(json.dumps({
        "pairs": report["paired"], "unpaired": len(report["unpaired"]),
        "baseline": [args.baseline, summary["pass_rate_baseline"]],
        "compare": [args.compare, summary["pass_rate_compare"]],
        "delta_pass_rate": summary["delta_pass_rate"],
        "mcnemar_p": summary["mcnemar"]["p_value"],
        "case_bootstrap": report["case_bootstrap"],
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
