"""Build the 2-2 LoRA training file from the annotated SFT pool.

Renders each pool entry as ``{"prompt", "completion"}`` with the prompt byte-identical
to the benchmark inference path (chat template applied with ``enable_thinking=False``,
ending in the closed empty think block), so mlx_lm's own template application — whose
default kwargs differ — cannot reintroduce a train/inference mismatch. Output goes
under output/ and is never committed; the annotated pool is the committed artifact.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_solver import GUIDED_SYSTEM_SUFFIX, TASK_GUIDANCE  # noqa: E402
from app.benchmark_reporting import SYSTEM_PROMPT  # noqa: E402

POOL_PATH = ROOT / "benchmarks/fewshot/2-2_sft_pool.jsonl"
PINNED_PATH = ROOT / "benchmarks/lawbench/zero_shot/2-2.json"


class ChatTemplateTokenizer(Protocol):
    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ) -> str: ...


def render(
    tokenizer: ChatTemplateTokenizer,
    instruction: str,
    sentence: str,
    label: str,
) -> dict[str, str]:
    system = SYSTEM_PROMPT + GUIDED_SYSTEM_SUFFIX + "\n本任务核对方法：" + TASK_GUIDANCE["2-2"]
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"{instruction}\n句子:{sentence}"}]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                           enable_thinking=False)
    assert prompt.endswith("<think>\n\n</think>\n\n"), "benchmark rendering drifted"
    return {"prompt": prompt, "completion": f"[争议焦点]{label}<eoa>"}


def build(pool_path: Path, pinned_path: Path, output: Path) -> dict[str, int]:
    pool = [json.loads(line) for line in pool_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pinned = json.loads(pinned_path.read_text(encoding="utf-8"))
    instruction = pinned[0]["instruction"].strip()
    if len({row["instruction"] for row in pinned}) != 1:
        raise ValueError("Pinned 2-2 instructions are not uniform; cannot render a single template")
    try:
        from mlx_lm import load  # type: ignore[import-not-found]  # optional training-environment dependency
    except ImportError as exc:  # repo venv has no mlx; build with the training venv
        raise SystemExit("run with output/lora-2-2-v1/venv/bin/python (needs mlx_lm)") from exc
    _, tokenizer = load("/Users/xiaoy/.omlx/models/Qwythos-9B-v2-8bit-mlx")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for item in pool:
            handle.write(json.dumps(render(tokenizer, instruction, item["sentence"], item["label"]),
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
