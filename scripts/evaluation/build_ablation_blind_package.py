"""Build a three-group blind review package from a recorded A/B/C ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.evaluation.ablation_review import COLLECTION_MODES, MIN_SAMPLE, build_package  # noqa: E402
from app.evaluation.collaboration_ablation import load_batch_dataset  # noqa: E402

EXIT_PROTOCOL = 2


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:  # exclusive: never overwrite a package
        stream.write(text)


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def readme(review: dict[str, Any], manifest: dict[str, Any]) -> str:
    return "\n".join([
        "# 三组盲审包",
        "",
        f"- 盲审版本：`{manifest['blind_version']}`；采集方式：`{manifest['collection_mode']}`。",
        f"- 样本：{manifest['items']} 题，覆盖 {manifest['cases']} 个案例；抽样规则 `{manifest['sample_rule']}`。",
        f"- 数据集摘要：`{manifest['dataset_sha256']}`；账本摘要：`{manifest['attempts_sha256']}`。",
        f"- 样本摘要：`{review['sample_sha256']}`。",
        "",
        "## 怎么读",
        "",
        "1. 先看 `review.json`：每题只有 X1/X2/X3 三份候选答案，组别未公开。",
        "2. 按要点完整性、无据断言、引用是否支撑、该拒答时是否拒答四项独立判断。",
        "3. 把通过/不通过与说明写进自己的标签文件；不要修改 `review.json`。",
        "4. `key.json` 由组织者单独保管，审核结束并锁定标签后再对照。",
        "",
        "## 边界",
        "",
        "- 题目与来源是合成材料，未经律师标注；本包不构成法律准确率结论。",
        "- 没有三组成功尝试的题目不会进入样本，也不会用替身补齐。",
        "- 若采集方式为 `stub`，本包只用于流程演练。",
    ]) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三组盲审包")
    parser.add_argument("--run", type=Path, help="含 attempts.jsonl 的批次目录")
    parser.add_argument("--attempts", type=Path, help="直接指定 attempts.jsonl")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="包目录，必须不存在")
    parser.add_argument("--groups", default="A,B,C")
    parser.add_argument("--size", type=int, default=MIN_SAMPLE)
    parser.add_argument("--seed", type=int, default=20261027)
    parser.add_argument("--collection-mode", choices=COLLECTION_MODES, default="real_model")
    parser.add_argument("--allow-stub", action="store_true", help="允许用替身答案演练流程")
    args = parser.parse_args(argv)

    attempts = args.attempts or (args.run / "attempts.jsonl" if args.run else None)
    if attempts is None or not attempts.is_file():
        print("缺少 --run/--attempts 指向的账本", file=sys.stderr)
        return EXIT_PROTOCOL
    groups = tuple(part.strip().upper() for part in args.groups.split(",") if part.strip())
    try:
        dataset = load_batch_dataset(args.dataset)
        rows = [json.loads(line) for line in attempts.read_text(encoding="utf-8").splitlines() if line.strip()]
        package = build_package(dataset, rows, groups=groups, size=args.size, seed=args.seed,
                                collection_mode=args.collection_mode, allow_stub=args.allow_stub)
    except ValueError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return EXIT_PROTOCOL
    try:
        args.output.mkdir(parents=True, mode=0o700)  # refuse an existing package
    except FileExistsError:
        print(f"{args.output} 已存在；盲审包不覆盖旧包", file=sys.stderr)
        return EXIT_PROTOCOL
    try:
        write_json(args.output / "review.json", package["review"])
        write_json(args.output / "key.json", package["key"])
        write_text(args.output / "README.md", readme(package["review"], package["manifest"]))
        write_json(args.output / "PACK_MANIFEST.json", {
            **package["manifest"],
            "files": {name: hashlib.sha256((args.output / name).read_bytes()).hexdigest()
                      for name in ("review.json", "key.json", "README.md")},
            "file_digest_basis": "sha256 of the written bytes",
            "attempts_file": str(attempts),
        })
    except FileExistsError as exc:
        print(f"包文件已存在，拒绝覆盖：{exc.filename}", file=sys.stderr)
        return EXIT_PROTOCOL
    print(json.dumps({"package": str(args.output), "items": package["manifest"]["items"],
                      "cases": package["manifest"]["cases"],
                      "eligible_pool": package["manifest"]["eligible_pool"],
                      "collection_mode": package["manifest"]["collection_mode"],
                      "review_sha256": package["manifest"]["review_sha256"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
