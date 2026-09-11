"""Compare statutory retrieval modes (lexical / dense / rrf / rerank) on real gold.

Builds a :class:`StatutoryIndex` over a canonical corpus (embedding every article
through the local service), assembles a dev/confirm dataset from pinned LawBench
citation gold, and reports the four modes through
:func:`app.statutory_benchmark.evaluate`. This is the retrieval-ranking counterpart
to ``scripts/statutory_gold_report.py`` (which scores the production lexical path).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.lawbench import load_task  # noqa: E402
from app.legal_corpus import LegalCorpus  # noqa: E402
from app.rag import EmbeddingClient, RerankClient  # noqa: E402
from app.statutory_benchmark import METRICS, evaluate  # noqa: E402
from app.statutory_gold import gold_citations  # noqa: E402
from app.statutory_hybrid import MODES, StatutoryHybrid  # noqa: E402
from app.statutory_index import IndexSpec, StatutoryIndex  # noqa: E402


def build_validity(corpus: LegalCorpus) -> dict[str, dict[str, Any]]:
    """One verified interval per document; only the newest version of a law is open.

    ponytail: effective_from falls back to version_date when the publication omits
    an effective date, and superseded versions close when the next one opens. That
    is enough to disambiguate today's corpus; real amendment history would need the
    official transition dates.
    """
    by_law: dict[str, list[dict[str, Any]]] = {}
    for doc in corpus.documents:
        by_law.setdefault(doc["law_name"], []).append(doc)
    validity: dict[str, dict[str, Any]] = {}
    for law, docs in by_law.items():
        ordered = sorted(docs, key=lambda item: item["effective_date"] or item["version_date"])
        for index, doc in enumerate(ordered):
            start = doc["effective_date"] or doc["version_date"]
            end = (ordered[index + 1]["effective_date"] or ordered[index + 1]["version_date"]) if index + 1 < len(ordered) else None
            validity[doc["document_id"]] = {"effective_from": start, "effective_to": end,
                                            "source": doc["source_url"]}
    return validity


def build_index(corpus: LegalCorpus, embedding: EmbeddingClient, spec: IndexSpec) -> StatutoryIndex:
    keys = [f"{doc['document_id']}/{article['article_id']}" for doc in corpus.documents for article in doc["articles"]]
    texts = [article["text"] for doc in corpus.documents for article in doc["articles"]]
    vectors: dict[str, list[float]] = {}
    batch = 32
    for start in range(0, len(texts), batch):
        chunk = texts[start:start + batch]
        result, _ = embedding.embed(chunk)
        if len(result) != len(chunk):
            raise RuntimeError(f"Embedding count mismatch at offset {start}")
        for key, vector in zip(keys[start:start + batch], result):
            vectors[key] = vector
    return StatutoryIndex(corpus, spec, vectors=vectors, validity=build_validity(corpus))


def build_dataset(task_id: str, index: StatutoryIndex, *, per_split: int, effective_date: str) -> dict[str, Any]:
    aliases: dict[str, str] = {}
    for doc in index.documents.values():
        for alias in {doc["law_name"], *doc["aliases"]}:
            aliases.setdefault(alias, doc["document_id"])
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in load_task(task_id):
        question = row["question"]
        gold: dict[str, int] = {}
        for law, number in gold_citations(task_id, question, row["answer"]):
            document_id = aliases.get(law) or aliases.get(f"中华人民共和国{law}") or aliases.get(law.removeprefix("中华人民共和国"))
            key = f"{document_id}/{number}" if document_id else None
            if key and key in index.rows:
                gold[key] = 1
        if not gold or question in seen:
            continue
        seen.add(question)
        tasks.append({"id": f"{task_id}-{len(tasks):04d}", "query": question, "gold": gold,
                      "query_effective_date": effective_date})
    if len(tasks) < 2 * per_split:
        raise ValueError(f"Not enough evaluable questions for {task_id}: {len(tasks)}")
    for position, item in enumerate(tasks):
        item["split"] = "dev" if position < per_split else "confirm"
    return {"tasks": tasks[:2 * per_split]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--task", default="3-1")
    parser.add_argument("--per-split", type=int, default=100)
    parser.add_argument("--effective-date", default="2026-01-01", help="query 适用日期，须晚于语料内所有生效日")
    parser.add_argument("--index-cache", type=Path, help="复用/保存向量索引")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    config = json.loads((ROOT / "benchmarks/statutory-experiment-v1.json").read_text())
    embedding = EmbeddingClient(prefer_remote=True, model="Qwen3-Embedding-4B-4bit-DWQ")
    corpus = LegalCorpus(args.corpus_dir)
    if args.index_cache and args.index_cache.exists():
        index = StatutoryIndex.load(args.index_cache, corpus, IndexSpec(embedding.model, 2560, "omlx"))
    else:
        index = build_index(corpus, embedding, IndexSpec(embedding.model, 2560, "omlx"))
        if args.index_cache:
            args.index_cache.parent.mkdir(parents=True, exist_ok=True)
            index.save(args.index_cache)
    dataset = build_dataset(args.task, index, per_split=args.per_split, effective_date=args.effective_date)
    engine = StatutoryHybrid(index, embedding=embedding, reranker=RerankClient())
    report = evaluate(engine, dataset, config)
    summary = {"corpus": str(args.corpus_dir), "task": args.task, "articles_indexed": len(index.rows),
               "questions": len(dataset["tasks"]), "per_split": args.per_split,
               "modes": list(MODES), "dev": {}, "confirm": {}, "promotion": report["promotion"]}
    for split in ("dev", "confirm"):
        for mode in MODES:
            entry = report[split]["summaries"][mode]
            summary[split][mode] = {
                "metrics": {name: (round(value, 4) if value is not None else None)
                            for name, value in entry["metrics"].items()},
                "available": entry["available"], "failure": entry["failure"],
                "p95_ms": round(entry["p95_ms"], 1) if entry["p95_ms"] is not None else None}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({**summary, "report": report}, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
