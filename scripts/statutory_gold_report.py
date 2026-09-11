"""Score statutory retrieval against extracted citation gold, offline and model-free.

For each pinned LawBench question this resolves the reference citation
(``app.statutory_gold``) to a frozen-corpus article, runs
``benchmark_retrieval.retrieve`` and reports hit@5 / MRR@10 / NDCG@10 with
``statutory_benchmark.metrics``. This separates "did retrieval find the right
article" from "did the generator's answer score well".
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_retrieval import retrieve  # noqa: E402
from app.lawbench import load_task  # noqa: E402
from app.legal_corpus import LegalCorpus  # noqa: E402
from app.statutory_benchmark import METRICS, metrics  # noqa: E402
from app.statutory_gold import gold_citations  # noqa: E402

DEFAULT_TASKS = ("1-1", "3-1", "3-2", "3-8")


def alias_index(corpora: list[LegalCorpus]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for corpus in corpora:
        for doc in corpus.documents:
            for alias in {doc["law_name"], *doc["aliases"]}:
                index.setdefault(alias, []).append(doc)
    return index


def resolve(citations: list[tuple[str, int]], index: dict[str, list[dict[str, Any]]],
            ) -> tuple[dict[str, int], list[str]]:
    """Map citations to gold article IDs; the newest version wins on duplicates.

    ponytail: version correctness is not graded here (current_law_300 covers it);
    any matching law/date selection only affects which version's article ID is compared.
    """
    gold: dict[str, int] = {}
    unresolved: list[str] = []
    documents = {doc["document_id"]: doc for docs in index.values() for doc in docs}
    for law, number in citations:
        docs = index.get(law) or [doc for doc in documents.values() if doc["law_name"].endswith(law)]
        if not docs:
            unresolved.append(f"{law}第{number}条")
            continue
        doc = max(docs, key=lambda item: item["version_date"])
        article_id = str(number)
        if not any(article["article_id"] == article_id for article in doc["articles"]):
            unresolved.append(f"{law}第{number}条")
            continue
        gold[f"{doc['document_id']}/{article_id}"] = 1
    return gold, unresolved


def evaluate_task(task_id: str, rows: list[dict[str, Any]], corpora: list[LegalCorpus],
                  directories: list[str], limit: int | None = None) -> dict[str, Any]:
    index = alias_index(corpora)
    modes: Counter[str] = Counter()
    per_metric: dict[str, list[float]] = {name: [] for name in METRICS}
    unresolved: Counter[str] = Counter()
    selected = rows if limit is None else rows[:limit]
    scored = 0
    for row in selected:
        gold, missing = resolve(gold_citations(task_id, row["question"], row["answer"]), index)
        if missing:
            unresolved.update(missing)
        if not gold:
            continue
        result = retrieve(task_id, row["question"], directories)
        modes[result["mode"]] += 1
        ranked = list(dict.fromkeys(f"{hit['document_id']}/{hit['article_id']}" for hit in result["hits"]))
        for name, value in metrics(ranked, gold).items():
            per_metric[name].append(value)
        scored += 1
    return {
        "task": task_id, "questions": len(selected), "scored": scored,
        "unscored": len(selected) - scored, "modes": dict(modes),
        "metrics": {name: (sum(values) / len(values) if values else None) for name, values in per_metric.items()},
        "unresolved_citations": dict(unresolved.most_common(20)),
        "unresolved_count": sum(unresolved.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", default=None, help="可重复：默认 1-1/3-1/3-2/3-8")
    parser.add_argument("--corpus-dir", type=Path, action="append", required=True, help="可重复：法条库目录")
    parser.add_argument("--limit", type=int, default=None, help="每个任务最多评测的题数")
    parser.add_argument("--output", type=Path, help="把完整结果写成 JSON")
    args = parser.parse_args()
    tasks = args.task or list(DEFAULT_TASKS)
    directories = [str(directory) for directory in args.corpus_dir]
    corpora = [LegalCorpus(directory) for directory in directories]
    report = {"corpus_directories": directories,
              "tasks": [evaluate_task(task, list(load_task(task)), corpora, directories, args.limit)
                        for task in tasks]}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
