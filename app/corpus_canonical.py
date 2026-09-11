"""Build one canonical statute corpus from many builder output directories.

Many ``legal_corpus*`` directories ship overlapping publications. This module merges
them into a single corpus containing exactly one document per
``(law_name, version_date)`` publication. It **selects** a source document and copies
its text verbatim: it never edits, fuses or re-normalizes legal text.

Selection is frozen, not positional:

1. Classification comes from :func:`app.corpus_audit.audit` (exact / cosmetic /
   boundary / substantive). A substantive conflict aborts the build.
2. Cosmetic and exact duplicates pick the highest-authority source
   (:data:`SOURCE_TIERS`); ties break on source directory then document ID so the
   output is deterministic.
3. A boundary difference picks the structurally correct copy (fewer heading leaks)
   rather than rewriting the leaky one.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from .corpus_audit import audit, dedup_digest, heading_leak_articles
from .legal_corpus import SCHEMA_VERSION, LegalCorpus

CANONICAL_SCHEMA_VERSION = "lexvault-canonical-laws-v1"
# Authority order, highest first. A pattern starting with "." matches that host
# suffix; a bare pattern must equal the host exactly. An unlisted host falls to the
# last tier. Authority only breaks ties between already-equivalent publications.
SOURCE_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("national_people_congress", (".npc.gov.cn",)),
    ("state_council", ("www.gov.cn", "gov.cn")),
    ("official_gov_cn", (".gov.cn",)),
    ("other_verified", ()),
)
REASON = {
    "unique_publication": "unique_publication",
    "exact_duplicate": "exact_duplicate",
    "cosmetic_duplicate": "cosmetic_duplicate",
    "boundary_difference": "boundary_preferred_source",
}
SEVERITY = ("substantive_conflict", "boundary_difference", "cosmetic_duplicate", "exact_duplicate")


def source_tier(source_url: str | None) -> int:
    host = (urlparse(source_url or "").hostname or "").lower()
    for index, (_, patterns) in enumerate(SOURCE_TIERS[:-1]):
        if any((host == pattern[1:] or host.endswith(pattern)) if pattern.startswith(".")
               else host == pattern for pattern in patterns):
            return index
    return len(SOURCE_TIERS) - 1


def _is_canonical(directory: str | Path) -> bool:
    manifest_path = Path(directory) / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        return "canonical_schema_version" in json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False


def _publications(directories: Sequence[str | Path]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for directory in directories:
        corpus = LegalCorpus(Path(directory))
        for entry, doc in zip(corpus.manifest["documents"], corpus.documents):
            groups[(doc["law_name"], doc["version_date"])].append({
                "source_dir": str(corpus.directory), "document": doc, "entry": entry,
                "articles": {article["article_id"]: article["text"] for article in doc["articles"]},
            })
    return groups


def _kind_by_key(report: dict[str, Any]) -> dict[tuple[str, str], str]:
    kinds: dict[tuple[str, str], str] = {}
    for record in report["conflicts"]:
        key = (record["law_name"], record["version_date"])
        current = kinds.get(key)
        if current is None or SEVERITY.index(record["difference_type"]) < SEVERITY.index(current):
            kinds[key] = record["difference_type"]
    return kinds


def select(candidates: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Choose the canonical copy; raises before writing on a substantive conflict."""
    if kind == "substantive_conflict":
        names = ", ".join(sorted(item["source_dir"] for item in candidates))
        raise ValueError(f"Substantive conflict cannot be canonicalized: {names}")
    if kind == "boundary_difference":
        return min(candidates, key=lambda item: (
            len(heading_leak_articles(item["articles"])), source_tier(item["document"].get("source_url")),
            item["source_dir"], item["document"]["document_id"]))
    return min(candidates, key=lambda item: (
        source_tier(item["document"].get("source_url")), item["source_dir"], item["document"]["document_id"]))


def _entry(chosen: dict[str, Any], others: list[dict[str, Any]], kind: str, document_file: str,
           document_sha256: str) -> dict[str, Any]:
    doc, source_entry = chosen["document"], chosen["entry"]
    return {
        "document_id": doc["document_id"], "document_file": document_file,
        "document_sha256": document_sha256,
        "raw_sha256": source_entry.get("raw_sha256"),
        "source_url": doc.get("source_url"), "publisher": doc.get("publisher"),
        "version_date": doc["version_date"], "law_name": doc["law_name"],
        "effective_date": doc.get("effective_date"), "version_status": doc.get("version_status"),
        "article_count": len(doc["articles"]),
        "canonical_source": chosen["source_dir"],
        "canonical_source_url": doc.get("source_url"),
        "canonical_document_sha256": source_entry.get("document_sha256"),
        "canonicalization_reason": REASON.get(kind, kind),
        "dedup_sha256": dedup_digest(chosen["articles"]),
        "equivalent_sources": [{
            "source": item["source_dir"], "document_id": item["document"]["document_id"],
            "source_url": item["document"].get("source_url"),
            "raw_sha256": item["entry"].get("raw_sha256"),
            "document_sha256": item["entry"].get("document_sha256"),
        } for item in sorted(others, key=lambda item: item["source_dir"])],
    }


def build(directories: Sequence[str | Path], output: str | Path) -> dict[str, Any]:
    """Write a canonical corpus and return its manifest; fail closed on real conflicts."""
    target = Path(output).resolve()
    # Never treat our own output as a source: a --root scan on a rerun would
    # otherwise ingest a previously built canonical directory as a duplicate.
    sources = [directory for directory in directories
               if Path(directory).resolve() != target and not _is_canonical(directory)]
    report = audit(sources)
    if report["classification_counts"]["substantive_conflict"]:
        raise ValueError("Refusing to build: substantive conflicts require manual review")
    kinds = _kind_by_key(report)
    groups = _publications(sources)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    entries: list[dict[str, Any]] = []
    structural_leaks: dict[str, list[str]] = {}
    for key in sorted(groups):
        candidates = groups[key]
        kind = kinds.get(key, "unique_publication")
        chosen = select(candidates, kind)
        others = [item for item in candidates if item is not chosen]
        document_file = f"{chosen['document']['document_id']}.json"
        raw = json.dumps(chosen["document"], ensure_ascii=False, allow_nan=False).encode()
        (target / document_file).write_bytes(raw)
        entries.append(_entry(chosen, others, kind, document_file, hashlib.sha256(raw).hexdigest()))
        leaks = heading_leak_articles(chosen["articles"])
        if leaks:
            structural_leaks[f"{key[0]} {key[1]}"] = leaks
    seen_ids = [entry["document_id"] for entry in entries]
    if len(seen_ids) != len(set(seen_ids)):
        raise ValueError("Canonical corpus has duplicate document IDs")
    manifest = {
        "schema_version": SCHEMA_VERSION, "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "source_corpora": [str(Path(directory)) for directory in sources],
        "source_policy": [name for name, _ in SOURCE_TIERS],
        "publication_count": len(entries), "duplicate_keys_merged": len(kinds),
        "documents": entries,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**manifest, "structural_leaks": structural_leaks,
            "duplicate_keys": report["duplicate_keys"],
            "classification_counts": report["classification_counts"]}
