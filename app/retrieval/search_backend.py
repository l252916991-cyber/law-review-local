"""Backend-specific halves of page retrieval: full-text candidates and page vectors.

``app/retrieval/rag.py`` owns ranking, fusion and diagnostics. The two queries whose SQL
cannot be shared live here:

* full text: SQLite FTS5 ``MATCH`` with ``bm25()``; PostgreSQL ``tsvector``
  with ``ts_rank`` over the same three columns and the same column weights;
* page vectors: SQLite returns every vector of the case and Python scores them;
  PostgreSQL scores inside the database with pgvector and returns only the top
  ``limit``. Both apply the same compatibility checks and the same filters, so
  the candidate set is the same; scores differ only by float32 rounding.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

#: ts_rank weights in {D, C, B, A} order; A/B/C carry document_name, doc_type
#: and text. PostgreSQL caps weights at 1.0, so the FTS5 bm25 column weights
#: 1.6 / 1.2 / 1.0 are divided by 1.6 (same ratios, same order).
_TS_RANK_WEIGHTS = "{0.0625,0.625,0.75,1.0}"


def is_postgres(conn: Any) -> bool:
    # Without a PostgreSQL connection the driver module was never imported, so
    # a SQLite process never pays psycopg's import cost (~0.1 s) here.
    module = sys.modules.get("app.storage.db_postgres")
    return module is not None and isinstance(conn, module.PgConnection)


def fts_page_candidates(conn: sqlite3.Connection, terms: list[str], case_id: int, limit: int) -> list[Any]:
    """Pages matching any term; ``bm25_score`` ascending is best first on both backends."""
    if is_postgres(conn):
        query = " || ".join("phraseto_tsquery('simple', ?)" for _ in terms)
        return conn.execute(
            f"""
            SELECT page_id, document_id, page_no, document_name AS name, doc_type, text,
                   -ts_rank('{_TS_RANK_WEIGHTS}', tsv, q.query) AS bm25_score
            FROM pages_fts, (SELECT ({query}) AS query) AS q
            WHERE case_id = ? AND tsv @@ q.query
            ORDER BY bm25_score, page_id LIMIT ?
            """,
            (*terms, case_id, limit),
        ).fetchall()
    match = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
    return conn.execute(
        """
        SELECT page_id, document_id, page_no, document_name AS name, doc_type, text,
               bm25(pages_fts, 0, 0, 0, 0, 1.6, 1.2, 1.0) AS bm25_score
        FROM pages_fts WHERE pages_fts MATCH ? AND case_id = ?
        ORDER BY bm25_score LIMIT ?
        """,
        (match, case_id, limit),
    ).fetchall()


def scored_page_vectors(
    conn: sqlite3.Connection, *, case_id: int, query_vector: list[float], backend: str, model: str,
    required_terms: list[str] | None, limit: int,
) -> tuple[list[tuple[float, dict[str, Any]]], int]:
    """Top pages by cosine similarity (> 0) and the number of incompatible vectors.

    A vector is compatible when it was made by the same backend and model with
    the query's dimensions and is a valid non-zero vector. ``required_terms``,
    when given, keeps only pages containing at least one of them (used for the
    hashed fallback, whose collisions are not evidence of relevance).
    """
    from app.retrieval.rag import cosine_similarity, decode_vector
    from app.storage.db import rowdict

    dimensions = len(query_vector)
    if is_postgres(conn):
        compatible = ("e.backend = ? AND e.model = ? AND e.dimensions = ? AND e.embedding IS NOT NULL "
                      "AND vector_dims(e.embedding) = ? AND vector_norm(e.embedding) > 0")
        compatible_params = (backend, model, dimensions, dimensions)
        skipped = conn.execute(
            f"""SELECT COUNT(*) FROM embedding_cache e JOIN pages p ON p.id = e.page_id
                JOIN documents d ON d.id = p.document_id WHERE d.case_id = ? AND NOT ({compatible})""",
            (case_id, *compatible_params),
        ).fetchone()[0]
        term_filter = ""
        term_params: tuple[str, ...] = ()
        if required_terms is not None:
            if not required_terms:
                return [], int(skipped)
            term_filter = "AND (" + " OR ".join("strpos(p.text, ?) > 0" for _ in required_terms) + ")"
            term_params = tuple(required_terms)
        literal = json.dumps(query_vector)
        rows = conn.execute(
            f"""
            SELECT p.id AS page_id, p.page_no, p.text, d.id AS document_id, d.name, d.doc_type,
                   e.backend, e.model, e.dimensions, 1 - (e.embedding <=> ?::vector) AS score
            FROM embedding_cache e JOIN pages p ON p.id = e.page_id
            JOIN documents d ON d.id = p.document_id
            WHERE d.case_id = ? AND {compatible} {term_filter}
              AND 1 - (e.embedding <=> ?::vector) > 0
            ORDER BY score DESC, p.id LIMIT ?
            """,
            (literal, case_id, *compatible_params, *term_params, literal, limit),
        ).fetchall()
        scored = []
        for row in rows:
            item = rowdict(row)
            scored.append((float(item.pop("score")), item))
        return scored, int(skipped)

    rows = conn.execute(
        """
        SELECT p.id AS page_id, p.page_no, p.text, d.id AS document_id,
               d.name, d.doc_type, e.vector_json, e.backend, e.model, e.dimensions
        FROM embedding_cache e JOIN pages p ON p.id = e.page_id
        JOIN documents d ON d.id = p.document_id WHERE d.case_id = ?
        """,
        (case_id,),
    ).fetchall()
    skipped = 0
    scored_items = []
    for row in rows:
        item = rowdict(row)
        page_vector = decode_vector(item.pop("vector_json"), dimensions)
        if (item["backend"] != backend or item["model"] != model or
                item["dimensions"] != dimensions or not page_vector):
            skipped += 1
            continue
        if required_terms is not None and not any(term in item["text"] for term in required_terms):
            continue
        score = cosine_similarity(query_vector, page_vector)
        if score > 0:
            scored_items.append((score, item))
    scored_items.sort(key=lambda value: -value[0])
    return scored_items[:limit], skipped
