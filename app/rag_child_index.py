"""Persistent, case-scoped page-child vector cache for exact-scan retrieval."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from typing import Any

from .db import connect, now, transaction
from .rag_chunks import CHUNK_PROFILES, CHUNK_VERSION, DEFAULT_CHUNK_PROFILE, page_chunks


MAX_CHILDREN = 10_000
EMBED_BATCH_SIZE = 32


def resolve_chunk_profile(name: str) -> dict[str, Any]:
    """Resolve a chunk profile to size/overlap/version for the index identity."""
    if name == DEFAULT_CHUNK_PROFILE:
        # Read CHUNK_VERSION through this module so a patched version still
        # invalidates the cache, as the existing tests rely on.
        return {"size": 400, "overlap": 50, "version": CHUNK_VERSION}
    profile = CHUNK_PROFILES.get(name)
    if profile is None:
        raise ValueError(f"Unknown child chunk profile: {name}")
    return dict(profile)


class ChildScanBudgetExceeded(ValueError):
    """The case child index cannot fit the exact-scan budget.

    A ``ValueError`` subclass so callers that only guard the experimental index
    keep working unchanged; the retriever catches it to fall back to page-level
    retrieval instead of failing the request.
    """


def _valid_vector(vector: Any, dimensions: int) -> bool:
    return (
        isinstance(vector, list)
        and len(vector) == dimensions
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in vector
        )
        and any(value != 0 for value in vector)
    )


def _decode_vector(encoded: str, dimensions: int) -> list[float]:
    try:
        vector = json.loads(encoded)
    except (TypeError, ValueError):
        return []
    return vector if _valid_vector(vector, dimensions) else []


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_snapshot(
    case_id: int,
    *,
    backend: str,
    dimensions: int,
    model_identity: str,
    chunk_version: str,
) -> tuple[
    list[dict[str, Any]],
    dict[int, dict[str, Any]],
    dict[int, str],
    list[int],
    dict[int, list[dict[str, Any]]],
]:
    """Read parent text, cache identity, and vectors from one transaction."""
    conn = connect()
    try:
        conn.execute("BEGIN")
        pages = [
            dict(row)
            for row in conn.execute(
                """
                SELECT p.id AS page_id, p.page_no, p.text, d.id AS document_id,
                       d.name, d.doc_type
                FROM pages p JOIN documents d ON d.id = p.document_id
                WHERE d.case_id = ? ORDER BY p.id
                """,
                (case_id,),
            )
        ]
        states = {
            int(row["page_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT s.* FROM page_child_index_state s
                JOIN pages p ON p.id = s.page_id
                JOIN documents d ON d.id = p.document_id
                WHERE d.case_id = ?
                """,
                (case_id,),
            )
        }
        page_hashes = {
            int(page["page_id"]): _content_hash(str(page["text"])) for page in pages
        }
        metadata_hits = [
            page_id
            for page_id, page_hash in page_hashes.items()
            if (
                (state := states.get(page_id)) is not None
                and state["page_hash"] == page_hash
                and state["chunk_version"] == chunk_version
                and state["model_identity"] == model_identity
                and state["backend"] == backend
                and int(state["dimensions"]) == dimensions
                and 0 <= int(state["child_count"]) <= MAX_CHILDREN
            )
        ]
        if sum(int(states[page_id]["child_count"]) for page_id in metadata_hits) > MAX_CHILDREN:
            raise ChildScanBudgetExceeded(f"Experimental child scan exceeds {MAX_CHILDREN} chunks")

        grouped: dict[int, list[dict[str, Any]]] = {page_id: [] for page_id in metadata_hits}
        for start in range(0, len(metadata_hits), 400):
            batch = metadata_hits[start:start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT page_id, ordinal, char_start, char_end, text, vector_json
                FROM page_child_chunks WHERE page_id IN ({placeholders})
                ORDER BY page_id, ordinal
                """,
                batch,
            )
            for row in rows:
                grouped[int(row["page_id"])].append(dict(row))
        return pages, states, page_hashes, metadata_hits, grouped
    finally:
        conn.close()


def _cached_children(
    page: dict[str, Any], rows: list[dict[str, Any]], dimensions: int,
) -> list[dict[str, Any]] | None:
    if [int(row["ordinal"]) for row in rows] != list(range(len(rows))):
        return None
    children = []
    parent_text = str(page["text"])
    for row in rows:
        start = int(row["char_start"])
        end = int(row["char_end"])
        vector = _decode_vector(str(row["vector_json"]), dimensions)
        if (
            start < 0
            or end <= start
            or end > len(parent_text)
            or not str(row["text"]).strip()
            or str(row["text"]) != parent_text[start:end]
            or not vector
        ):
            return None
        children.append(
            {
                **page,
                "parent_text": parent_text,
                "ordinal": int(row["ordinal"]),
                "char_start": start,
                "char_end": end,
                "text": str(row["text"]),
                "vector": vector,
            }
        )
    return children


def load_or_build_child_index(
    case_id: int,
    *,
    backend: str,
    dimensions: int,
    model_identity: str,
    embed: Callable[[list[str]], tuple[list[list[float]], str]],
    chunk_profile: str = DEFAULT_CHUNK_PROFILE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return current children, rebuilding only invalid or stale parent pages."""
    if dimensions < 1 or not backend or not model_identity:
        raise ValueError("Invalid child embedding space")
    profile = resolve_chunk_profile(chunk_profile)
    chunk_version = str(profile["version"])

    pages, states, page_hashes, metadata_hits, cached_rows = _read_snapshot(
        case_id,
        backend=backend,
        dimensions=dimensions,
        model_identity=model_identity,
        chunk_version=chunk_version,
    )
    reused_by_page: dict[int, list[dict[str, Any]]] = {}
    pending_pages: list[dict[str, Any]] = []
    for page in pages:
        page_id = int(page["page_id"])
        state = states.get(page_id)
        rows = cached_rows.get(page_id, [])
        children = _cached_children(page, rows, dimensions) if page_id in metadata_hits else None
        if children is not None and state and len(children) == int(state["child_count"]):
            reused_by_page[page_id] = children
        else:
            pending_pages.append(page)

    built_by_page: dict[int, list[dict[str, Any]]] = {}
    pending_children: list[dict[str, Any]] = []
    for page in pending_pages:
        children = [
            {
                **page,
                "parent_text": str(page["text"]),
                **chunk,
                "ordinal": ordinal,
            }
            for ordinal, chunk in enumerate(
                page_chunks(str(page["text"]), profile["size"], int(profile["overlap"]))
            )
        ]
        built_by_page[int(page["page_id"])] = children
        pending_children.extend(children)

    reused_count = sum(len(children) for children in reused_by_page.values())
    if reused_count + len(pending_children) > MAX_CHILDREN:
        raise ChildScanBudgetExceeded(f"Experimental child scan exceeds {MAX_CHILDREN} chunks")

    vectors: list[list[float]] = []
    for start in range(0, len(pending_children), EMBED_BATCH_SIZE):
        batch = pending_children[start:start + EMBED_BATCH_SIZE]
        embedded, actual_backend = embed([str(child["text"]) for child in batch])
        if (
            actual_backend != backend
            or len(embedded) != len(batch)
            or not all(_valid_vector(vector, dimensions) for vector in embedded)
        ):
            raise RuntimeError("embedding_backend_changed_during_child_index")
        vectors.extend(embedded)
    for child, vector in zip(pending_children, vectors):
        child["vector"] = vector

    if pending_pages:
        with transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = {
                int(row["page_id"]): dict(row)
                for row in conn.execute(
                    """
                    SELECT p.id AS page_id, p.document_id, p.page_no, p.text, d.case_id,
                           d.name, d.doc_type
                    FROM pages p JOIN documents d ON d.id = p.document_id
                    WHERE d.case_id = ?
                    """,
                    (case_id,),
                )
            }
            for page in pages:
                page_id = int(page["page_id"])
                parent = current.get(page_id)
                if (
                    parent is None
                    or int(parent["case_id"]) != case_id
                    or int(parent["document_id"]) != int(page["document_id"])
                    or int(parent["page_no"]) != int(page["page_no"])
                    or str(parent["text"]) != str(page["text"])
                    or str(parent["name"]) != str(page["name"])
                    or str(parent["doc_type"]) != str(page["doc_type"])
                    or _content_hash(str(parent["text"])) != page_hashes[page_id]
                ):
                    raise RuntimeError("page_changed_during_child_index_build")
            for page in pending_pages:
                page_id = int(page["page_id"])
                children = built_by_page[page_id]
                conn.execute("DELETE FROM page_child_index_state WHERE page_id = ?", (page_id,))
                conn.execute(
                    """
                    INSERT INTO page_child_index_state(
                        page_id, page_hash, chunk_version, model_identity, backend,
                        dimensions, child_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        page_id,
                        page_hashes[page_id],
                        chunk_version,
                        model_identity,
                        backend,
                        dimensions,
                        len(children),
                        now(),
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO page_child_chunks(
                        page_id, ordinal, char_start, char_end, text, vector_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            page_id,
                            int(child["ordinal"]),
                            int(child["char_start"]),
                            int(child["char_end"]),
                            str(child["text"]),
                            json.dumps(child["vector"], separators=(",", ":")),
                        )
                        for child in children
                    ],
                )

    children = []
    for page in pages:
        page_id = int(page["page_id"])
        children.extend(reused_by_page.get(page_id, built_by_page.get(page_id, [])))
    return children, {
        "indexed_page_count": len(pages),
        "rebuilt_page_count": len(pending_pages),
        "reused_page_count": len(pages) - len(pending_pages),
        "embedded_child_count": len(pending_children),
        "reused_child_count": reused_count,
        "child_chunk_profile": chunk_profile,
        "chunk_version": chunk_version,
    }
