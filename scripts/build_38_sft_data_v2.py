"""Freeze round-2 3-8 SFT data: complete samples only, prompt/answer fields.

Reuses the round-1 frozen semantic clusters and split seed so train/valid/holdout
stay comparable; within each split only samples whose prompt+answer+eos encode
within max_seq are kept (no truncation, eos always supervised). Quality gates:
nonempty answer, no repetitive template answers. Renders {"text", "prompt"}
without the eos token — the training wrapper appends it once and masks the
prompt via a token-level offset (encode(prompt) is the inference-time encoding,
so prompt and answer are tokenized separately to keep that boundary exact).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_38_sft_data import OUT_DIR as ROUND1_DIR  # noqa: E402

OUT = ROOT / "output/sft-38-v2"
MAX_SEQ = 512
MAX_REPEAT_RATIO = 0.3


def repeat_ratio(text: str, n: int = 8) -> float:
    grams = [text[i:i + n] for i in range(max(0, len(text) - n + 1))]
    return 1 - len(set(grams)) / len(grams) if grams else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round1-manifest", type=Path, default=ROUND1_DIR / "manifest.json")
    args = parser.parse_args()

    from mlx_lm import load  # type: ignore[import-not-found]  # training venv

    _, tok = load(str(ROOT / "models/Qwythos-9B-v2-4bit-mlx"))

    # Rebuild the round-1 rows in split order: the cluster file stores candidate
    # indices and round-1 assigned them with the same seed, so re-deriving the
    # assignment keeps splits identical.
    import random

    manifest = json.loads(args.round1_manifest.read_text())
    if manifest["split_seed"] != "38-sft-v1":
        raise ValueError("unexpected round-1 seed")
    round1_rows = [json.loads(line)["text"] for line in (ROUND1_DIR / "dataset/train.jsonl").read_text().splitlines()]
    round1_valid = [json.loads(line)["text"] for line in (ROUND1_DIR / "dataset/valid.jsonl").read_text().splitlines()]
    round1_holdout = [json.loads(line)["text"] for line in (ROUND1_DIR / "holdout.jsonl").read_text().splitlines()]

    marker = "<think>\n\n</think>\n\n"
    stats: dict[str, dict[str, Any]] = {}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "dataset").mkdir(exist_ok=True)
    for name, texts in (("train", round1_rows), ("valid", round1_valid), ("holdout", round1_holdout)):
        kept, dropped = [], {"too_long": 0, "empty_answer": 0, "repetitive": 0}
        for text in texts:
            prompt, answer = text.split(marker, 1)
            answer = answer.replace("<|im_end|>", "").strip()
            if not answer:
                dropped["empty_answer"] += 1
                continue
            if repeat_ratio(answer) > MAX_REPEAT_RATIO:
                dropped["repetitive"] += 1
                continue
            prompt_tokens = len(tok.encode(prompt + marker))
            answer_tokens = len(tok.encode(answer))
            if prompt_tokens + answer_tokens + 1 > MAX_SEQ:  # +1 eos
                dropped["too_long"] += 1
                continue
            kept.append({"prompt": prompt + marker, "answer": answer})
        path = OUT / "dataset" / f"{name}.jsonl" if name != "holdout" else OUT / f"{name}.jsonl"
        with path.open("w") as handle:
            for row in kept:
                handle.write(json.dumps({"text": row["prompt"] + row["answer"],
                                         "prompt": row["prompt"], "answer": row["answer"]},
                                        ensure_ascii=False) + "\n")
        cite = sum(1 for row in kept if re.search(r"《[^》]{2,30}》", row["answer"]))
        stats[name] = {"kept": len(kept), "dropped": dropped,
                       "citation_rate": round(cite / len(kept), 4) if kept else None,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        print(name, json.dumps(stats[name], ensure_ascii=False), flush=True)

    print(f"total kept: {sum(s['kept'] for s in stats.values())}", flush=True)
    (OUT / "manifest.json").write_text(json.dumps({
        "version": "3-8-sft-round2-v1", "max_seq": MAX_SEQ,
        "quality_rules": {"empty_answer": True, "repeat_ratio_gt": MAX_REPEAT_RATIO},
        "cluster_source": str(ROUND1_DIR / "clusters.json"), "split_seed": "38-sft-v1",
        "format": {"text": "prompt+answer without eos", "eos": "appended by training wrapper",
                   "mask": "offset = len(encode(prompt)); answer+eos supervised"},
        "splits": stats,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
