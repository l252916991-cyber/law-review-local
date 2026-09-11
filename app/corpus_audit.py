"""Read-only audit of duplicate statute publications across corpus directories.

Separate builder runs can ship the same ``(law_name, version_date)`` publication with
different stored content. Before such directories are merged into one canonical corpus
each duplicate must be classified: an exact or cosmetic duplicate can be canonicalized
automatically, a structural heading leak points at a parser boundary bug, and a
genuinely different text must fail closed for manual review.

This module reads corpora through :class:`LegalCorpus` (so file hashes are verified),
selects nothing, resolves no canonical source and writes nothing. It only reports.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Sequence

from .legal_corpus import LegalCorpus

SCHEMA_VERSION = "corpus-conflict-audit-v1"
# A structural heading the parser should have kept as a boundary, not glued to a body.
HEADING_LINE = re.compile(r"^(?:第[零〇一二三四五六七八九十百千万两0-9]+[编章节](?:\s|$)|附\s*则$)")
CAPTION = re.compile(r"【[^】]*】")
NOISE = re.compile(r"[\s\u3000，。；：、（）()\[\]“”‘’\"'`.,;:!?！？\-—…]+")
ACTIONS = {
    "exact_duplicate": "KEEP_ONE_DROP_DUPLICATE",
    "cosmetic_duplicate": "AUTO_CANONICALIZE",
    "boundary_difference": "FIX_BUILDER_OR_CANONICALIZE_WITH_BOUNDARY_RULE",
    "substantive_conflict": "FAIL_CLOSED_MANUAL_REVIEW",
}


def _basic(text: str) -> str:
    """Fullwidth/whitespace projection: catches line-wrap and punctuation-width churn."""
    return re.sub(r"[\s\u3000]+", " ", unicodedata.normalize("NFKC", text)).strip()


def _semantic(text: str) -> str:
    """Dedup projection: drops captions and every punctuation/space, keeping only characters."""
    return NOISE.sub("", CAPTION.sub("", unicodedata.normalize("NFKC", text)))


def _body(text: str) -> str:
    """Semantic projection with structural heading lines removed (boundary comparison)."""
    kept = [line for line in text.splitlines() if not HEADING_LINE.match(line.strip())]
    return _semantic("\n".join(kept))


def _headings(text: str) -> set[str]:
    return {re.sub(r"\s+", "", line) for line in text.splitlines() if HEADING_LINE.match(line.strip())}


def _hash(articles: dict[str, str], project: Callable[[str], str]) -> str:
    payload = sorted((key, project(text)) for key, text in articles.items())
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def _publication(directory: Path, entry: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_dir": str(directory), "document_id": doc["document_id"],
        "source_url": doc.get("source_url"), "article_count": len(doc["articles"]),
        "manifest_document_sha256": entry.get("document_sha256"),
        "manifest_raw_sha256": entry.get("raw_sha256"),
        "articles": {article["article_id"]: article["text"] for article in doc["articles"]},
    }


def classify(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Classify one duplicate pair; never merges or prefers a source."""
    ids_a, ids_b = set(a["articles"]), set(b["articles"])
    raw_a, raw_b = _hash(a["articles"], lambda t: t), _hash(b["articles"], lambda t: t)
    basic_a, basic_b = _hash(a["articles"], _basic), _hash(b["articles"], _basic)
    sem_a, sem_b = _hash(a["articles"], _semantic), _hash(b["articles"], _semantic)
    body_a, body_b = _hash(a["articles"], _body), _hash(b["articles"], _body)

    common = sorted(ids_a & ids_b)
    different = [key for key in common if _semantic(a["articles"][key]) != _semantic(b["articles"][key])]
    headings = sorted({h for key in different for h in _headings(a["articles"][key])}
                      ^ {h for key in different for h in _headings(b["articles"][key])})
    if raw_a == raw_b:
        kind = "exact_duplicate"
    elif sem_a == sem_b:
        kind = "cosmetic_duplicate"
    elif ids_a == ids_b and body_a == body_b:
        kind = "boundary_difference"
    else:
        kind = "substantive_conflict"
    return {
        "source_a": a["source_dir"], "document_id_a": a["document_id"], "source_url_a": a["source_url"],
        "source_b": b["source_dir"], "document_id_b": b["document_id"], "source_url_b": b["source_url"],
        "article_count_a": a["article_count"], "article_count_b": b["article_count"],
        "article_id_set_equal": ids_a == ids_b,
        "raw_hash_equal": raw_a == raw_b,
        "basic_normalized_hash_equal": basic_a == basic_b,
        "semantic_normalized_hash_equal": sem_a == sem_b,
        "raw_sha256_a": raw_a, "raw_sha256_b": raw_b,
        "dedup_sha256_a": sem_a, "dedup_sha256_b": sem_b,
        "manifest_document_sha256_a": a["manifest_document_sha256"],
        "manifest_document_sha256_b": b["manifest_document_sha256"],
        "difference_type": kind, "different_article_ids": different, "heading_difference": headings,
        "recommended_action": ACTIONS[kind],
    }


def audit(directories: Sequence[str | Path]) -> dict[str, Any]:
    """Classify every duplicate publication key across the given corpus directories."""
    corpora = [LegalCorpus(Path(directory)) for directory in directories]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    scanned = 0
    for corpus in corpora:
        for entry, doc in zip(corpus.manifest["documents"], corpus.documents):
            groups[(doc["law_name"], doc["version_date"])].append(_publication(corpus.directory, entry, doc))
            scanned += 1
    records: list[dict[str, Any]] = []
    duplicate_keys = 0
    for (law_name, version_date), publications in sorted(groups.items()):
        by_content: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for publication in publications:
            by_content[_hash(publication["articles"], lambda t: t)].append(publication)
        variants = list(by_content.values())
        if len(variants) == 1 and len(variants[0]) == 1:
            continue
        duplicate_keys += 1
        pairs = [(variants[0][0], variants[0][1])] if len(variants) == 1 else [
            (left[0], right[0]) for left, right in combinations(variants, 2)]
        for a, b in pairs:
            record = classify(a, b)
            record["law_name"], record["version_date"] = law_name, version_date
            if len(variants) == 1:
                record["all_source_dirs"] = sorted(item["source_dir"] for item in variants[0])
            records.append(record)
    counts = Counter(record["difference_type"] for record in records)
    return {
        "schema_version": SCHEMA_VERSION, "corpus_directories": [str(corpus.directory) for corpus in corpora],
        "documents_scanned": scanned, "duplicate_keys": duplicate_keys, "pair_count": len(records),
        "classification_counts": {kind: counts.get(kind, 0) for kind in ACTIONS},
        "conflicts": records,
    }
