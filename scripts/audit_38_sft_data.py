"""Dedup audit between LawBench 3-8 and candidate consultation SFT corpora.

Read-only data engineering: it inspects benchmark questions to quantify
leakage, never to build prompts. Exact match uses normalized text; near
duplicates use symmetric character 8-gram containment so paraphrased copies
of the same consultation cannot slip through.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GRAM = 8
STRIP = re.compile(r"\s+")

NEAR_DUPLICATE = 0.5
WATCH = 0.3


def grams(text: str) -> set[str]:
    value = STRIP.sub("", text)
    return {value[i:i + GRAM] for i in range(max(0, len(value) - GRAM + 1))}


def containment(a: set[str], b: set[str]) -> float:
    """Symmetric containment: the smaller set's covered fraction."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def load_bench_questions() -> dict[str, str]:
    task = json.loads((ROOT / "benchmarks/lawbench/zero_shot/3-8.json").read_text())
    return {f"3-8_{index:04d}": item["question"] for index, item in enumerate(task)}


def load_disc_pair(path: Path) -> list[tuple[str, str]]:
    return [(row["id"], row["input"])
            for line in path.read_text().splitlines()
            for row in [json.loads(line)]]


def load_disc_triplet(path: Path) -> list[tuple[str, str]]:
    return [(row["id"] + "_triplet", row["input"])
            for line in path.read_text().splitlines()
            for row in [json.loads(line)]]


def load_lawyer_llama(path: Path) -> list[tuple[str, str]]:
    """Consultation-style items only: exam questions are a different distribution."""
    data = json.loads(path.read_text())
    return [(f"lawyer_llama_{index}", item["instruction"])
            for index, item in enumerate(data)
            if not item.get("source", "").startswith("judical_examination")]


FILES = {
    "disc_pair_qa": "DISC-Law-SFT-Pair-QA-released.jsonl",
    "disc_triplet_qa": "DISC-Law-SFT-Triplet-QA-released.jsonl",
    "lawyer_llama_consult": "lawyer_llama_all.json",
}


def audit(candidates: list[tuple[str, str]], bench: dict[str, str]) -> dict:
    bench_grams = {qid: grams(text) for qid, text in bench.items()}
    inverted: dict[str, list[str]] = {}
    for qid, grams_set in bench_grams.items():
        for gram in grams_set:
            inverted.setdefault(gram, []).append(qid)
    normalized_bench = {STRIP.sub("", text): qid for qid, text in bench.items()}
    exact, near, watch = [], [], []
    for cid, text in candidates:
        normalized = STRIP.sub("", text)
        if normalized in normalized_bench:
            exact.append((cid, normalized_bench[normalized]))
            continue
        candidate_grams = grams(text)
        best_qid, best = None, 0.0
        for gram in candidate_grams:
            for qid in inverted.get(gram, ()):
                score = containment(candidate_grams, bench_grams[qid])
                if score > best:
                    best_qid, best = qid, score
        if best_qid is None:
            continue
        record = (cid, best_qid, round(best, 3))
        if best >= NEAR_DUPLICATE:
            near.append(record)
        elif best >= WATCH:
            watch.append(record)
    return {"candidates": len(candidates), "exact": exact, "near_duplicate": near,
            "watch": watch, "clean": len(candidates) - len(exact) - len(near)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", type=Path, default=ROOT / "output/sft-audit-38")
    args = parser.parse_args()
    bench = load_bench_questions()
    corpora: dict[str, dict] = {}
    for name, filename in FILES.items():
        path = args.audit_dir / filename
        if not path.exists():
            print(f"skip {name}: missing {path}", file=sys.stderr)
            continue
        result = audit(LOADERS[name](path), bench)
        corpora[name] = result
        summary = {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()}
        print(name, json.dumps(summary, ensure_ascii=False))
        for tag in ("exact", "near_duplicate", "watch"):
            for record in result[tag][:5]:
                print(f"  [{tag}] {record}")
    report = {"bench_questions": len(bench), "gram": GRAM,
              "near_threshold": NEAR_DUPLICATE, "watch_threshold": WATCH,
              "corpora": corpora}
    (args.audit_dir / "dedup_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    return 0


LOADERS = {
    "disc_pair_qa": load_disc_pair,
    "disc_triplet_qa": load_disc_triplet,
    "lawyer_llama_consult": load_lawyer_llama,
}


if __name__ == "__main__":
    raise SystemExit(main())
