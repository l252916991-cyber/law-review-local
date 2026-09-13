"""Build the 3-8 LoRA round-1 dataset: clean, group, split, render, freeze.

Pipeline (single run, one frozen manifest):
1. Clean the DISC Pair-QA pool with the audited rules (exam-style inputs,
   truncated answers, the five leakage-edge ids). Citation rate is recorded as
   a stratification field only — never a filter.
2. Embed every input, union-find clusters at cosine >= threshold so paraphrase
   clusters never straddle train/validation/holdout splits.
3. Greedily assign whole clusters to holdout (200), trainval (200), train
   (3000) with a frozen seed; the remainder stays unused (round-2 pool).
4. Render text-format rows byte-identical to the benchmark inference path
   (task_guided 3-8 system, closed empty think block) so mlx_lm cannot
   reintroduce a train/inference template mismatch.

Gold LawBench answers are never read here; benchmark questions are excluded
by the audited dedup step, and the manifest records every rule.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


PAIR_FILE = ROOT / "output/sft-audit-38/DISC-Law-SFT-Pair-QA-released.jsonl"
OUT_DIR = ROOT / "output/sft-38-v1"

EXAM = re.compile(r"请给出详细的推理过程|以下陈述是否正确|判断其所属|下列(选项|说法|哪些)|单选|多选")
END_PUNCT = tuple("。!?!?;;")
LEAKAGE_EDGE_IDS = [
    "legal_question_answering_5620", "legal_question_answering_5580",
    "legal_question_answering_14980", "legal_question_answering_51278",
    "legal_question_answering_51631",
]
CITE = re.compile(r"《[^》]{2,40}》第[零〇一二三四五六七八九十百千万两0-9]+条")

# Prompt constants are read as literals from the source modules so this script
# stays importable in the training venv (no app dependency chain) while keeping
# a single source of truth; values are hashed into the manifest.
CONSTANT_SOURCES = ["app/benchmark_solver.py", "app/benchmark_reporting.py"]


def literal_from_source(filename: str, name: str) -> Any:
    import ast

    tree = ast.parse((ROOT / filename).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise KeyError(f"{name} not found in {filename}")


SYSTEM_PROMPT = literal_from_source("app/benchmark_reporting.py", "SYSTEM_PROMPT")
GUIDED_SYSTEM_SUFFIX = literal_from_source("app/benchmark_solver.py", "GUIDED_SYSTEM_SUFFIX")
TASK_GUIDANCE_38 = literal_from_source("app/benchmark_solver.py", "TASK_GUIDANCE")["3-8"]

CLUSTER_THRESHOLD = 0.85
SIZES = {"holdout": 200, "valid": 200, "train": 3000}
SEED = "38-sft-v1"
EMBED_BATCH = 64


def clean_pool() -> list[dict]:
    rows = []
    drop = {"exam": 0, "truncated": 0, "leakage": 0, "short": 0}
    for line in PAIR_FILE.read_text().splitlines():
        row = json.loads(line)
        if row["id"] in LEAKAGE_EDGE_IDS:
            drop["leakage"] += 1
            continue
        if EXAM.search(row["input"]):
            drop["exam"] += 1
            continue
        if not row["output"].rstrip().endswith(END_PUNCT):
            drop["truncated"] += 1
            continue
        if len(row["input"].strip()) < 10:
            drop["short"] += 1
            continue
        rows.append({"id": row["id"], "input": row["input"], "output": row["output"],
                     "has_citation": bool(CITE.search(row["output"]))})
    print(f"clean pool: {len(rows)} (dropped {drop})", flush=True)
    return rows


EMBED_URL = os.getenv("LAW_REVIEW_EMBEDDING_URL", "http://127.0.0.1:8000/v1")
EMBED_MODEL = os.getenv("LAW_REVIEW_EMBEDDING_MODEL", "Qwen3-Embedding-4B-4bit-DWQ")


def embed_inputs(rows: list[dict]) -> list[list[float]]:
    """Call the local oMLX /v1/embeddings directly; no app dependency chain."""
    import urllib.request

    # urllib honors system proxies; the local oMLX endpoint must bypass them
    # (known 502 failure mode in this environment).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    endpoint = EMBED_URL.rstrip("/") + "/embeddings"
    vectors: list[list[float]] = []
    for start in range(0, len(rows), EMBED_BATCH):
        batch = [row["input"] for row in rows[start:start + EMBED_BATCH]]
        body = json.dumps({"model": EMBED_MODEL, "input": batch}).encode()
        request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=120) as response:
            payload = json.load(response)
        part = [item["embedding"] for item in payload["data"]]
        if len(part) != len(batch):
            raise RuntimeError(f"embedding returned {len(part)} for {len(batch)} inputs at {start}")
        vectors.extend(part)
        if (start // EMBED_BATCH) % 50 == 0:
            print(f"embedded {start + len(batch)}/{len(rows)}", flush=True)
    return vectors


def cluster(rows: list[dict], vectors: list[list[float]]) -> list[list[int]]:
    """Union-find over cosine >= threshold; returns member index clusters.

    Blockwise numpy matmul: the full 64K x 64K matrix would not fit memory.
    Run with the training venv (output/lora-2-2-v1/venv) which has numpy.
    """
    import numpy as np  # type: ignore[import-not-found]

    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1e-12
    matrix = matrix / norms[:, None]
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    block = 512
    for start in range(0, len(rows), block):
        sims = matrix[start:start + block] @ matrix.T
        rows_idx, cols_idx = np.nonzero(sims >= CLUSTER_THRESHOLD)
        for r, c in zip(rows_idx.tolist(), cols_idx.tolist()):
            i, j = start + r, c
            if i < j:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
        if (start // block) % 20 == 0:
            print(f"clustered {start + block}/{len(rows)}", flush=True)
    groups: dict[int, list[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def assign(clusters: list[list[int]], rng: random.Random) -> dict[str, list[int]]:
    rng.shuffle(clusters)
    fills = {name: SIZES[name] for name in SIZES}
    assignment: dict[str, list[int]] = {name: [] for name in SIZES}
    unused: list[int] = []
    exhausted = False
    for cluster in clusters:
        target = None if exhausted else next((name for name in SIZES if fills[name] > 0), None)
        if target is None:
            exhausted = True
            unused.extend(cluster)
            continue
        assignment[target].extend(cluster)
        fills[target] -= len(cluster)
    for name in SIZES:
        if fills[name] > 0:
            raise RuntimeError(f"not enough clusters to fill {name}: short {fills[name]}")
    assignment["unused"] = unused
    return assignment


_TOKENIZER = None


def _tokenizer() -> Any:
    """mlx_lm's tokenizer for the 4bit base, identical to the 2-2 pipeline."""
    global _TOKENIZER
    if _TOKENIZER is None:
        from mlx_lm import load  # type: ignore[import-not-found]

        _, _TOKENIZER = load(str(ROOT / "models/Qwythos-9B-v2-4bit-mlx"))
    return _TOKENIZER


def render_prompt(instruction: str, question: str) -> str:
    tokenizer = _tokenizer()
    system = SYSTEM_PROMPT + GUIDED_SYSTEM_SUFFIX + "\n本任务核对方法：" + TASK_GUIDANCE_38
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"{instruction.strip()}\n{question}"}]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                           enable_thinking=False)
    assert prompt.endswith("<think>\n\n</think>\n\n"), "benchmark rendering drifted"
    return prompt


INSTRUCTION = "请回答下列问题，首先给出回答，然后给出对应的法律依据: "


def write_split(path: Path, rows: list[dict], indices: list[int]) -> None:
    """mlx_lm expects --data to be a directory of train/valid/test jsonl files."""
    with path.open("w") as handle:
        for i in indices:
            row = rows[i]
            prompt = render_prompt(INSTRUCTION, row["input"])
            handle.write(json.dumps({"text": prompt + row["output"] + "<|im_end|>"},
                                    ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-embed", action="store_true", help="reuse cached cluster file")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = clean_pool()
    cache = OUT_DIR / "clusters.json"
    if args.skip_embed and cache.exists():
        cached = json.loads(cache.read_text())
        clusters = cached["clusters"]
        vectors_digest = cached["vectors_sha256"]
    else:
        vectors = embed_inputs(rows)
        vectors_digest = hashlib.sha256(
            json.dumps(vectors, separators=(",", ":")).encode()).hexdigest()
        clusters = cluster(rows, vectors)
        cache.write_text(json.dumps({"clusters": clusters, "vectors_sha256": vectors_digest}))
    sizes = sorted((len(c) for c in clusters), reverse=True)
    print(f"clusters: {len(clusters)} | largest {sizes[:5]} | singletons {sum(1 for s in sizes if s == 1)}",
          flush=True)

    rng = random.Random(SEED)
    assignment = assign([list(c) for c in clusters], rng)
    order = {name: sorted(indices) for name, indices in assignment.items()}
    dataset_dir = OUT_DIR / "dataset"
    dataset_dir.mkdir(exist_ok=True)
    for name in ("holdout", "valid", "train"):
        target = dataset_dir / f"{name}.jsonl" if name in {"train", "valid"} else OUT_DIR / f"{name}.jsonl"
        write_split(target, rows, order[name])
    citation_rate = {name: round(statistics.fmean(rows[i]["has_citation"] for i in order[name]), 4)
                     for name in ("holdout", "valid", "train")}
    manifest = {
        "version": "3-8-sft-round1-v1",
        "source_file": str(PAIR_FILE),
        "source_sha256": hashlib.sha256(PAIR_FILE.read_bytes()).hexdigest(),
        "clean_rules": {"exam_style_regex": EXAM.pattern, "truncated_answer": True,
                        "leakage_edge_ids": LEAKAGE_EDGE_IDS, "min_input_chars": 10,
                        "citation_rate_used_as": "stratification-record-only"},
        "pool_size": len(rows),
        "constant_sources": CONSTANT_SOURCES,
        "prompt_sha256": hashlib.sha256((SYSTEM_PROMPT + GUIDED_SYSTEM_SUFFIX + TASK_GUIDANCE_38).encode()).hexdigest(),
        "embedding": {"model": EMBED_MODEL, "url": EMBED_URL, "batch": EMBED_BATCH,
                      "vectors_sha256": vectors_digest},
        "cluster": {"threshold_cosine": CLUSTER_THRESHOLD, "n_clusters": len(clusters),
                    "largest": sizes[:5], "singletons": sum(1 for s in sizes if s == 1)},
        "split_seed": SEED, "sizes": {k: len(order[k]) for k in order},
        "citation_rate": citation_rate,
        "splits_sha256": {name: hashlib.sha256(
            (dataset_dir / f"{name}.jsonl" if name in {"train", "valid"} else OUT_DIR / f"{name}.jsonl").read_bytes()
        ).hexdigest() for name in ("holdout", "valid", "train")},
        "instruction": INSTRUCTION,
        "rendering": {"system": "task_guided 3-8 (production prompt A)",
                      "enable_thinking": False, "eos": "<|im_end|>"},
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in manifest.items() if k != "splits_sha256"},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
