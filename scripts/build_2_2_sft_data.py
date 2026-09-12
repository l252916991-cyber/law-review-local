"""Build the 2-2 LoRA training file from the annotated SFT pool.

Renders each pool entry into the exact chat format the benchmark uses at inference
time (same system guidance, same instruction prefix, same [争议焦点]...<eoa> answer
format), so there is no train/inference mismatch. Output goes under output/ and is
never committed; the annotated pool itself is the committed artifact.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_solver import GUIDED_SYSTEM_SUFFIX, TASK_GUIDANCE  # noqa: E402
from app.benchmark_reporting import SYSTEM_PROMPT  # noqa: E402

POOL_PATH = ROOT / "benchmarks/fewshot/2-2_sft_pool.jsonl"
PINNED_PATH = ROOT / "benchmarks/lawbench/zero_shot/2-2.json"


def render(instruction: str, sentence: str, label: str) -> dict[str, list[dict[str, str]]]:
    system = SYSTEM_PROMPT + GUIDED_SYSTEM_SUFFIX + "\n本任务核对方法：" + TASK_GUIDANCE["2-2"]
    return {"messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{instruction}\n句子:{sentence}"},
        {"role": "assistant", "content": f"[争议焦点]{label}<eoa>"},
    ]}


def build(pool_path: Path, pinned_path: Path, output: Path) -> dict[str, int]:
    pool = [json.loads(line) for line in pool_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pinned = json.loads(pinned_path.read_text(encoding="utf-8"))
    instruction = pinned[0]["instruction"].strip()
    if len({row["instruction"] for row in pinned}) != 1:
        raise ValueError("Pinned 2-2 instructions are not uniform; cannot render a single template")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for item in pool:
            handle.write(json.dumps(render(instruction, item["sentence"], item["label"]),
                                    ensure_ascii=False) + "\n")
    return {"examples": len(pool)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/lora-2-2-v1/train.jsonl")
    args = parser.parse_args(argv)
    result = build(POOL_PATH, PINNED_PATH, args.output)
    print(json.dumps({**result, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
