"""Hybrid retrieval and persistent semantic memory.

The implementation intentionally keeps orchestration framework-agnostic: SQLite
FTS5 provides BM25 keyword recall, the local oMLX embedding endpoint provides
semantic recall, and Reciprocal Rank Fusion combines the two ranked lists.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .db import connect, now, transaction
from .services import (
    LOCAL_LLM_URL,
    assert_model_endpoint_allowed,
    best_quote,
    concise,
    egress_opener,
    query_terms,
    rowdict,
    search_pages,
)


EMBEDDING_MODEL = "Qwen3-Embedding-4B-4bit-DWQ"
EMBEDDING_TEXT_VERSION = "raw-text-v1"
HASHED_MODEL = "hashed-bigram-v1"
logger = logging.getLogger(__name__)


def embedding_identity(model: str, backend: str) -> str:
    """Dimensions alone do not identify a vector space."""
    versioned_model = HASHED_MODEL if backend == "hashed-local" else f"{model}@{os.getenv('LAW_REVIEW_EMBEDDING_REVISION', 'unspecified')}"
    return f"{versioned_model}|{EMBEDDING_TEXT_VERSION}"


def valid_vector(vector: Any, dimensions: int | None = None) -> bool:
    return (
        isinstance(vector, list) and bool(vector)
        and (dimensions is None or len(vector) == dimensions)
        and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) for value in vector)
        and any(value != 0 for value in vector)
    )


def decode_vector(encoded: str, dimensions: int | None = None) -> list[float]:
    try:
        vector = json.loads(encoded)
        return vector if valid_vector(vector, dimensions) else []
    except (TypeError, ValueError):
        return []


def expand_retrieval_query(query: str) -> str:
    """Add domain concepts without inserting case-specific answers."""
    additions: list[str] = []
    if any(term in query for term in ("总额", "金额", "多少", "人数", "几人")):
        additions.extend(["累计流入", "人民币", "投资人", "共收到", "银行流水"])
    if any(term in query for term in ("不知情", "不知道", "辩解", "反驳")):
        additions.extend(["不知道", "不清楚", "客观记录", "电子邮件", "要求", "确认", "回复", "谁决定", "收益率", "最后确认"])
    if any(term in query for term in ("审批", "固定回报", "保本保息")):
        additions.extend(["宣传稿", "电子邮件", "回复", "执行", "年化收益"])
    return " ".join([query, *additions])


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


def hashed_embedding(text: str, dimensions: int = 384) -> list[float]:
    """Deterministic offline fallback used when the local embedding server is down."""
    vector = [0.0] * dimensions
    normalized = re.sub(r"\s+", "", text.lower())
    tokens = [normalized[i : i + 2] for i in range(max(1, len(normalized) - 1))]
    tokens.extend(query_terms(text))
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * (1.0 + min(len(token), 8) / 8)
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class EmbeddingClient:
    def __init__(self, prefer_remote: bool = True, model: str | None = None):
        self.prefer_remote = prefer_remote
        self.model = model or os.getenv("LAW_REVIEW_EMBEDDING_MODEL", EMBEDDING_MODEL)
        self.base_url = os.getenv("LAW_REVIEW_EMBEDDING_URL", os.getenv("LAW_REVIEW_LLM_URL", LOCAL_LLM_URL)).rstrip("/")
        self.last_failure: str | None = None

    def embed(self, texts: list[str]) -> tuple[list[list[float]], str]:
        if not texts:
            return [], "none"
        self.last_failure = None
        if self.prefer_remote:
            try:
                # Case page text must never reach an unapproved destination.
                assert_model_endpoint_allowed(self.base_url)
                payload = json.dumps({"model": self.model, "input": texts}, ensure_ascii=False).encode("utf-8")
                request = urllib.request.Request(
                    f"{self.base_url}/embeddings",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                opener = egress_opener()
                with opener.open(request, timeout=120) as response:
                    body = json.load(response)
                ordered = sorted(body["data"], key=lambda item: item["index"])
                vectors = [item["embedding"] for item in ordered]
                if (len(vectors) == len(texts) and vectors
                        and [item["index"] for item in ordered] == list(range(len(texts)))
                        and all(valid_vector(vector, len(vectors[0])) for vector in vectors)):
                    return vectors, "omlx"
                raise ValueError("invalid_embedding_response")
            except (urllib.error.URLError, OSError, KeyError, TypeError, ValueError) as exc:
                self.last_failure = type(exc).__name__
                # Exception text may include a URL, document text or response body.
                logger.warning("embedding_fallback error_type=%s", self.last_failure)
        return [hashed_embedding(text) for text in texts], "hashed-local"


class HybridRetriever:
    def __init__(self, case_id: int, prefer_remote_embeddings: bool = True):
        self.case_id = case_id
        self.embedding_client = EmbeddingClient(prefer_remote_embeddings)
        self.vector_diagnostics: dict[str, Any] = {}
        self.keyword_diagnostics: dict[str, Any] = {}

    def ensure_vector_index(
        self, force: bool = False, *, backend: str | None = None, dimensions: int | None = None,
    ) -> dict[str, Any]:
        conn = connect()
        try:
            pages = [
                rowdict(row)
                for row in conn.execute(
                    """
                    SELECT p.id AS page_id, p.text FROM pages p
                    JOIN documents d ON d.id = p.document_id
                    WHERE d.case_id = ? ORDER BY p.id
                    """,
                    (self.case_id,),
                )
            ]
            cache = {
                row["page_id"]: rowdict(row)
                for row in conn.execute(
                    """SELECT e.* FROM embedding_cache e JOIN pages p ON p.id = e.page_id
                    JOIN documents d ON d.id = p.document_id WHERE d.case_id = ?""",
                    (self.case_id,),
                )
            }
        finally:
            conn.close()

        # Select the query's space before checking the cache. This also probes
        # remote recovery instead of permanently retaining an offline cache.
        if backend is None or dimensions is None:
            probe, backend = self.embedding_client.embed(["embedding-space-probe"])
            dimensions = len(probe[0])
        model = embedding_identity(self.embedding_client.model, backend)
        pending = []
        for page in pages:
            content_hash = hashlib.sha256(page["text"].encode("utf-8")).hexdigest()
            cached = cache.get(page["page_id"])
            if (force or not cached or cached["content_hash"] != content_hash
                    or cached["model"] != model or cached["backend"] != backend
                    or cached["dimensions"] != dimensions
                    or not decode_vector(cached["vector_json"], dimensions)):
                pending.append({**page, "content_hash": content_hash})

        space_changed = False
        if pending:
            if backend == "hashed-local":
                vectors = [hashed_embedding(item["text"]) for item in pending]
            else:
                vectors, actual_backend = self.embedding_client.embed([item["text"] for item in pending])
                if (actual_backend != backend or len(vectors) != len(pending)
                        or not all(valid_vector(vector, dimensions) for vector in vectors)):
                    # A backend outage/model reload between query and page
                    # embedding must not leave a mixed index or compare spaces.
                    space_changed = True
                    backend, dimensions = "hashed-local", 384
                    model = embedding_identity(self.embedding_client.model, backend)
                    pending = [{**page, "content_hash": hashlib.sha256(page["text"].encode("utf-8")).hexdigest()}
                               for page in pages]
                    vectors = [hashed_embedding(item["text"]) for item in pending]
            with transaction() as conn:
                for item, vector in zip(pending, vectors):
                    conn.execute(
                        """
                        INSERT INTO embedding_cache(page_id, model, dimensions, content_hash, vector_json, backend, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(page_id) DO UPDATE SET model=excluded.model, dimensions=excluded.dimensions,
                        content_hash=excluded.content_hash, vector_json=excluded.vector_json,
                        backend=excluded.backend, updated_at=excluded.updated_at
                        """,
                        (
                            item["page_id"],
                            model,
                            len(vector),
                            item["content_hash"],
                            json.dumps(vector, separators=(",", ":")),
                            backend,
                            now(),
                        ),
                    )
        return {
            "pages": len(pages),
            "embedded": len(pending),
            "cached": len(pages) - len(pending),
            "dimensions": dimensions,
            "backend": backend,
            "model": model,
            "space_changed": space_changed,
        }

    def keyword_search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        self.keyword_diagnostics = {"fts_available": True}
        terms = [term for term in query_terms(query) if len(term) >= 2][:12]
        if not terms:
            return search_pages(self.case_id, query, limit)
        match = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT page_id, document_id, page_no, document_name AS name, doc_type, text,
                       bm25(pages_fts, 0, 0, 0, 0, 1.6, 1.2, 1.0) AS bm25_score
                FROM pages_fts WHERE pages_fts MATCH ? AND case_id = ?
                ORDER BY bm25_score LIMIT ?
                """,
                (match, self.case_id, limit),
            ).fetchall()
            results = [rowdict(row) for row in rows]
        except Exception as exc:
            self.keyword_diagnostics = {"fts_available": False, "error_type": type(exc).__name__}
            logger.warning("fts_fallback case_id=%s error_type=%s", self.case_id, type(exc).__name__)
            results = []
        finally:
            conn.close()
        # unicode61 does not segment every Chinese legal phrase consistently.
        # Fuse FTS5's BM25 rank with the application's Chinese bigram lexical
        # rank so exact entities and longer concepts both remain recallable.
        lexical = search_pages(self.case_id, query, max(limit, 20))
        fused: dict[tuple[int, int], dict[str, Any]] = {}
        for channel, weight, candidates in (("fts5", 1.0, results), ("legal-bigram", 0.75, lexical)):
            for rank, item in enumerate(candidates, 1):
                key = (int(item["document_id"]), int(item["page_no"]))
                target = fused.setdefault(key, {**item, "keyword_score": 0.0, "keyword_components": []})
                target["keyword_score"] += weight / (30 + rank)
                target["keyword_components"].append(channel)
        ranked = sorted(fused.values(), key=lambda item: -item["keyword_score"])
        matches = query_terms(query)
        for rank, item in enumerate(ranked[:limit], 1):
            item["keyword_rank"] = rank
            item["quote"] = best_quote(item["text"], matches)
        return ranked[:limit]

    def vector_search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        vectors, backend = self.embedding_client.embed([query])
        if not vectors:
            return []
        query_vector = vectors[0]
        query_failure = self.embedding_client.last_failure
        index = self.ensure_vector_index(backend=backend, dimensions=len(query_vector))
        if index["backend"] != backend:
            backend = "hashed-local"
            query_vector = hashed_embedding(query)
        model = embedding_identity(self.embedding_client.model, backend)
        self.vector_diagnostics = {
            "backend": backend, "model": model, "dimensions": len(query_vector),
            "degraded": backend == "hashed-local", "incompatible_vectors_skipped": 0,
            "fallback_reason": ("embedding_space_changed" if index["space_changed"] else
                                "remote_embedding_unavailable" if query_failure else
                                "offline_embeddings_requested" if not self.embedding_client.prefer_remote else None),
        }
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT p.id AS page_id, p.page_no, p.text, d.id AS document_id,
                       d.name, d.doc_type, e.vector_json, e.backend, e.model, e.dimensions
                FROM embedding_cache e JOIN pages p ON p.id = e.page_id
                JOIN documents d ON d.id = p.document_id WHERE d.case_id = ?
                """,
                (self.case_id,),
            ).fetchall()
        finally:
            conn.close()
        scored = []
        for row in rows:
            item = rowdict(row)
            page_vector = decode_vector(item.pop("vector_json"), len(query_vector))
            if (item["backend"] != backend or item["model"] != model or
                    item["dimensions"] != len(query_vector) or not page_vector):
                self.vector_diagnostics["incompatible_vectors_skipped"] += 1
                continue
            if backend == "hashed-local" and not any(
                term in item["text"] for term in query_terms(query) if len(term) >= 2
            ):
                # Hash collisions are not evidence of semantic relevance.
                continue
            score = cosine_similarity(query_vector, page_vector)
            if score > 0:
                item["vector_score"] = round(score, 6)
                item["query_embedding_backend"] = backend
                item["quote"] = best_quote(item["text"], query_terms(query))
                scored.append((score, item))
        scored.sort(key=lambda value: -value[0])
        output = [item for _, item in scored[:limit]]
        for rank, item in enumerate(output, 1):
            item["vector_rank"] = rank
        return output

    def retrieve(self, query: str, limit: int = 6) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        started = time.perf_counter()
        expanded_query = expand_retrieval_query(query)
        keyword = self.keyword_search(expanded_query, max(limit * 3, 12))
        vector = self.vector_search(expanded_query, max(limit * 3, 12))
        fused: dict[tuple[int, int], dict[str, Any]] = {}
        rrf_k = 60
        # The deterministic hashed fallback is deliberately cheap and useful as
        # a recall channel, but it is not a semantic model and must not outrank
        # exact legal terms, amounts or document names.  A real local embedding
        # model retains the original higher fusion weight.
        vector_backend = vector[0].get("query_embedding_backend") if vector else "none"
        vector_weight = 0.35 if vector_backend == "hashed-local" else 0.85
        for channel, weight, results in (("bm25", 1.0, keyword), ("vector", vector_weight, vector)):
            for rank, item in enumerate(results, 1):
                key = (int(item["document_id"]), int(item["page_no"]))
                target = fused.setdefault(key, {**item, "rrf_score": 0.0, "channels": []})
                target["rrf_score"] += weight / (rrf_k + rank)
                target["channels"].append(channel)
                for field in ("keyword_rank", "vector_rank", "vector_score", "query_embedding_backend"):
                    if field in item:
                        target[field] = item[field]
        ranked = sorted(fused.values(), key=lambda item: (-item["rrf_score"], item["document_id"], item["page_no"]))
        for rank, item in enumerate(ranked[:limit], 1):
            item["rank"] = rank
            item["rrf_score"] = round(item["rrf_score"], 6)
            item["retrieval_explain"] = (
                f"RRF={item['rrf_score']}; BM25#{item.get('keyword_rank', '-')}; "
                f"Vector#{item.get('vector_rank', '-')}"
            )
        metrics = {
            "mode": "FTS5/BM25 + Legal Lexical + Vector + RRF",
            "retrieval_mode": "hybrid_rrf",
            "source_count": len(ranked[:limit]),
            "keyword_candidates": len(keyword),
            "vector_candidates": len(vector),
            "vector_weight": vector_weight,
            "fused_candidates": len(fused),
            "query_expansion": expanded_query if expanded_query != query else "none",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "degraded": bool(self.vector_diagnostics.get("degraded")) or not self.keyword_diagnostics.get("fts_available", True),
            "embedding": dict(self.vector_diagnostics),
            "keyword": dict(self.keyword_diagnostics),
        }
        return ranked[:limit], metrics


def remember(
    case_id: int,
    user_name: str,
    content: str,
    source_run_id: int,
    importance: float = 0.7,
    prefer_remote_embeddings: bool = True,
    *,
    validated: bool = False,
) -> int:
    if not validated:
        raise ValueError("Only structurally validated review drafts may enter agent memory")
    content = concise(content, 1200)
    client = EmbeddingClient(prefer_remote_embeddings)
    vectors, backend = client.embed([content])
    vector = vectors[0] if vectors else []
    with transaction() as conn:
        if not conn.execute(
            "SELECT 1 FROM agent_runs WHERE id=? AND case_id=?", (source_run_id, case_id)
        ).fetchone():
            raise ValueError("Memory source run does not belong to this case")
        conn.execute(
            """
            INSERT INTO memories(case_id, user_name, kind, content, vector_json, embedding_model,
                                 importance, source_run_id, created_at)
            VALUES (?, ?, 'agent_draft_validated', ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                case_id,
                user_name,
                content,
                json.dumps(vector, separators=(",", ":")),
                f"{embedding_identity(client.model, backend)}|{backend}|{len(vector)}",
                max(0.0, min(1.0, importance)),
                source_run_id,
                now(),
            ),
        )
        row = conn.execute(
            "SELECT id FROM memories WHERE source_run_id=? AND case_id=?", (source_run_id, case_id)
        ).fetchone()
        if row is None:
            raise ValueError("Memory source run does not belong to this case")
        return row[0]


def recall_memories(
    case_id: int, query: str, limit: int = 3, prefer_remote_embeddings: bool = True
) -> list[dict[str, Any]]:
    client = EmbeddingClient(prefer_remote_embeddings)
    vectors, backend = client.embed([query])
    query_vector = vectors[0] if vectors else []
    identity = f"{embedding_identity(client.model, backend)}|{backend}|{len(query_vector)}"
    conn = connect()
    try:
        rows = [
            rowdict(row)
            for row in conn.execute(
                """SELECT m.* FROM memories m JOIN agent_runs r ON r.id=m.source_run_id AND r.case_id=m.case_id
                   WHERE m.case_id = ? AND m.kind='agent_draft_validated' AND r.status='completed'
                   ORDER BY m.id DESC LIMIT 100""", (case_id,)
            )
        ]
    finally:
        conn.close()
    scored = []
    comparable: list[tuple[dict[str, Any], list[float]]] = []
    remote_mismatches: list[dict[str, Any]] = []
    for item in rows:
        encoded = item.pop("vector_json")
        vector = decode_vector(encoded, len(query_vector))
        if item["embedding_model"] != identity or not vector:
            # Recall is read-only: re-embed a mismatched draft in the query's
            # space without mutating historical memory or mixing vector models.
            if backend == "hashed-local":
                vector = hashed_embedding(item["content"])
            else:
                remote_mismatches.append(item)
                continue
        comparable.append((item, vector))
    # Avoid one HTTP request per historical memory. Chunks keep request bodies
    # bounded while preserving the read-only, no-vector-space-mixing contract.
    for start in range(0, len(remote_mismatches), 16):
        chunk = remote_mismatches[start : start + 16]
        replacements, replacement_backend = client.embed([item["content"] for item in chunk])
        if replacement_backend != backend or len(replacements) != len(chunk):
            continue
        comparable.extend(
            (item, vector) for item, vector in zip(chunk, replacements)
            if valid_vector(vector, len(query_vector))
        )
    for item, vector in comparable:
        score = cosine_similarity(query_vector, vector)
        if score > 0:
            item["similarity"] = round(score, 5)
            item["trust"] = "structural_checks_only_lawyer_review_required"
            scored.append((score * (0.8 + item["importance"] * 0.2), item))
    scored.sort(key=lambda value: -value[0])
    return [item for _, item in scored[:limit]]
