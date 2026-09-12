"""Read-only quality report for frozen 3-8 experiment arms.

Complements the recorded ROUGE-L with detection checks: missed sub-questions
(interrogative count vs. reply sentences), statute citations that the local
corpus cannot support, and redundancy/length drift. Gold answers are only used
for length comparison and sample display, never scored here.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_metrics import score_lawbench_item
from app.benchmark_postprocess import normalize_consultation_surface
from app.benchmark_retrieval import corpus_fingerprint
from app.legal_corpus import LegalCorpus, article_number

CITATION = re.compile(r"《([^》]{2,40})》第([零〇一二三四五六七八九十百千万两0-9]+)条")
STRIPPED = " \n\t*#>-、。;；:：,，()（）[]【】"


def load_supported(directory: str) -> dict[str, set[int]]:
    """Map normalized law names to the set of article numbers they contain."""
    supported: dict[str, set[int]] = {}
    for doc in LegalCorpus(Path(directory)).documents:
        numbers = {article["article_number"] for article in doc["articles"]}
        for name in {doc["law_name"], *doc.get("aliases", [])}:
            supported.setdefault(name.replace("中华人民共和国", ""), set()).update(numbers)
    return supported


def citation_support(prediction: str, corpora: list[dict[str, set[int]]]) -> tuple[int, int, list[str]]:
    total, supported, unsupported = 0, 0, []
    for match in CITATION.finditer(prediction):
        total += 1
        name = match.group(1).replace("中华人民共和国", "").strip(STRIPPED)
        number = article_number(match.group(2))
        if any(name in book and number in book[name] for book in corpora):
            supported += 1
        else:
            unsupported.append(f"《{match.group(1)}》第{match.group(2)}条")
    return total, supported, unsupported


def repeated_ratio(text: str, n: int = 8) -> float:
    """Share of n-grams that repeat inside the same prediction."""
    grams = [text[i:i + n] for i in range(0, max(0, len(text) - n + 1))]
    return round(1 - len(set(grams)) / len(grams), 3) if grams else 0.0


def arm_report(run_dir: Path, corpora: list[dict[str, set[int]]], references: dict[str, str]) -> dict:
    rows = [json.loads(line) for line in (run_dir / "detailed_results.jsonl").read_text().splitlines()]
    scored = [row for row in rows if row.get("error") is None]
    per_question = {}
    for row in scored:
        normalized = score_lawbench_item("3-8", normalize_consultation_surface(row["prediction"]),
                                         row["reference"], question=row["question"]).score
        per_question[row["question_id"]] = {"raw": round(row["score"], 4), "normalized": round(normalized, 4)}
    citations_total, citations_supported, missed = 0, 0, 0
    unsupported_examples: list[str] = []
    for row in scored:
        total, supported, unsupported = citation_support(row["prediction"], corpora)
        citations_total += total
        citations_supported += supported
        unsupported_examples += unsupported[:2]
        questions = len(re.findall(r"[??？]", row["question"])) or 1
        sentences = len([s for s in re.split(r"[。;；]", row["prediction"].split("法律依据")[0]) if s.strip()])
        if sentences < questions:
            missed += 1
    stats = {
        "run": run_dir.name,
        "n": len(rows), "errors": len(rows) - len(scored),
        "mean_score": round(statistics.fmean(row["score"] for row in scored), 4) if scored else None,
        # Unified re-scoring: task_guided arms bypass the runner's 3-8 surface
        # postprocess, so every arm is re-scored on the normalized prediction.
        "mean_score_normalized": round(statistics.fmean(
            score_lawbench_item("3-8", normalize_consultation_surface(row["prediction"]),
                                row["reference"], question=row["question"]).score for row in scored), 4) if scored else None,
        "markdown_left_raw": sum(1 for row in scored if re.search(r"\*\*|^#{1,6}\s|^\s*[-*•]\s", row["prediction"], re.M)),
        "truncated": sum(1 for row in rows if row.get("finish_reason") == "length"),
        "rankers": dict(Counter(row.get("retrieval", {}).get("ranker") for row in rows)),
        "pred_chars_p50": int(statistics.median(len(row["prediction"]) for row in scored)),
        "ref_chars_p50": int(statistics.median(len(references[row["question_id"]]) for row in scored)),
        "redundancy_p50": statistics.median(repeated_ratio(row["prediction"]) for row in scored),
        "citations_total": citations_total, "citations_supported": citations_supported,
        "unsupported_examples": unsupported_examples[:8],
        "missed_reply_heuristic": missed,
        "per_question": per_question,
    }
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", type=Path, required=True)
    parser.add_argument("--corpora", nargs="+", type=str, required=True)
    args = parser.parse_args()
    print("loading corpora...", file=sys.stderr)
    corpora = [load_supported(directory) for directory in args.corpora]
    fingerprint = corpus_fingerprint(args.corpora)
    for run in args.runs:
        campaign = run.parent.parent
        references = {row["question_id"]: row["reference"]
                      for row in (json.loads(line) for line in (campaign / "references.jsonl").read_text().splitlines())}
        report = arm_report(run, corpora, references)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"corpus fingerprints: {len(fingerprint)} files", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
