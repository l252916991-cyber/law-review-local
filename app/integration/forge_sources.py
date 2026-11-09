"""Stdlib-only statutory source contract shared by host and isolated worker.

These identities describe a pinned public corpus, never an invented case page.
Validation establishes source/quote integrity, not legal or semantic correctness.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

STATUTE_SOURCE_VERSION = 1
_MARKER = re.compile(r"\[statute:(\d+)\]")
_QUOTED = re.compile(r"\[statute:(\d+)\]「([^」]*)」")


def statute_source(hit: dict[str, Any], corpus_version: str, index: int) -> dict[str, Any]:
    text = hit.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 20_000:
        raise ValueError("statute text is empty or exceeds the source bound")
    source = {"schema_version": STATUTE_SOURCE_VERSION, "kind": "statute",
              "index": index, "corpus_version": corpus_version,
              "document_id": hit.get("document_id"), "law_name": hit.get("law_name"),
              "version_date": hit.get("version_date"), "article_number": hit.get("article_number"),
              "subarticle_number": hit.get("subarticle_number"), "source_url": hit.get("source_url"),
              "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    _validate_source(source)
    return source


def _validate_source(source: dict[str, Any]) -> None:
    if not isinstance(source, dict):
        raise ValueError("statute source must be an object")
    if (source.get("schema_version") != STATUTE_SOURCE_VERSION or source.get("kind") != "statute"
            or type(source.get("index")) is not int or source["index"] < 1
            or type(source.get("article_number")) is not int or source["article_number"] < 1):
        raise ValueError("invalid statute source identity")
    for key in ("document_id", "law_name", "version_date", "corpus_version", "source_url"):
        if not isinstance(source.get(key), str) or not source[key].strip():
            raise ValueError("incomplete statute source identity")
    if not re.fullmatch(r"[0-9a-f]{64}", source["corpus_version"]):
        raise ValueError("invalid pinned corpus identity")
    text = source.get("text")
    if (not isinstance(text, str) or not text.strip() or len(text) > 20_000
            or hashlib.sha256(text.encode()).hexdigest() != source.get("text_sha256")):
        raise ValueError("statute text differs from its digest or exceeds bounds")


def verify_statutes(content: str, sources: list[dict[str, Any]], *,
                   corpus_version: str | None = None) -> tuple[bool, str]:
    """Fail closed on unquoted, forged, duplicated or stale statutory identities."""
    try:
        if len(sources) > 20:
            raise ValueError("too many statute sources")
        by_index = {}
        for source in sources:
            _validate_source(source)
            if corpus_version is not None and source["corpus_version"] != corpus_version:
                raise ValueError("statute is from a different pinned corpus")
            if source["index"] in by_index:
                raise ValueError("duplicate statute source index")
            by_index[source["index"]] = source
        markers, quotes = _MARKER.findall(content), _QUOTED.findall(content)
        if not markers or len(markers) != len(quotes):
            return False, "statutory answer requires an exact quote for every marker"
        for number, quote in quotes:
            source = by_index.get(int(number))
            if source is None or not quote.strip() or quote not in source["text"]:
                return False, "statute quote or identity is unavailable"
    except (ValueError, TypeError, KeyError):
        return False, "statute source contract is invalid"
    return True, "statute identity, pinned corpus and exact quote verified; semantics not assessed"
