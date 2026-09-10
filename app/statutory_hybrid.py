"""Independent experimental orchestration; no default clients or product wiring."""
from __future__ import annotations

import math
from collections import Counter
from time import perf_counter
from typing import Any, Literal, Protocol

from .legal_corpus import _terms
from .statutory_index import StatutoryIndex

Mode = Literal["lexical", "dense", "rrf", "rerank"]
MODES: tuple[Mode, ...] = ("lexical", "dense", "rrf", "rerank")


class EmbeddingClient(Protocol):
    model: str
    last_failure: str | None

    def embed(self, texts: list[str]) -> tuple[list[list[float]], str]: ...


class RerankClient(Protocol):
    model: str
    last_failure: str | None

    def score(self, query: str, documents: list[str]) -> list[float] | None: ...


class StatutoryHybrid:
    def __init__(self, index: StatutoryIndex, *, embedding: EmbeddingClient | None = None,
                 reranker: RerankClient | None = None, candidates: int = 48,
                 rrf_k: int = 60, soft_boost: float = 0.1) -> None:
        if candidates < 10 or rrf_k < 1 or not math.isfinite(soft_boost) or soft_boost < 0:
            raise ValueError("Invalid retrieval parameters")
        self.index, self.embedding, self.reranker = index, embedding, reranker
        self.candidates, self.rrf_k, self.soft_boost = candidates, rrf_k, soft_boost

    def search(self, query: str, *, mode: Mode = "lexical", explicit_law: str | None = None,
               inferred_law: str | None = None, query_effective_date: str | None = None,
               limit: int = 10) -> dict[str, Any]:
        if mode not in MODES or limit < 1 or limit > self.candidates:
            raise ValueError("Invalid mode or result limit")
        started = perf_counter()
        result: dict[str, Any] = {"mode": mode, "available": False, "failure": None, "hits": [],
                                  "issues": [], "embedding_calls": 0, "rerank_candidates": 0}
        try:
            keys, issues = self.index.eligible(explicit_law=explicit_law, query_effective_date=query_effective_date)
            result["issues"] = issues
            if not keys:
                raise ValueError("no_verified_candidates")
            if issues:
                raise ValueError("incomplete_validity")
            query_terms = _terms(query)
            terms = {key: _terms(self.index.rows[key]["text"]) for key in keys}
            df = Counter(term for counts in terms.values() for term in counts)
            lexical = {key: sum((1 + math.log(counts[t])) * (math.log((len(keys) + 1) / (df[t] + 1)) + 1)
                                for t in query_terms if counts[t]) / math.sqrt(sum(counts.values()) or 1)
                       for key, counts in terms.items()}

            positions = {key: position for position, key in enumerate(keys)}

            def ranked(scores: dict[str, float]) -> list[str]:
                return sorted(scores, key=lambda key: (-scores[key], positions[key]))[:self.candidates]

            dense: dict[str, float] = {}
            if mode != "lexical":
                if self.embedding is None or not self.index.vectors:
                    raise ValueError("embedding_unavailable")
                if self.embedding.model != self.index.spec.embedding_model:
                    raise ValueError("embedding_model_mismatch")
                result["embedding_calls"] += 1
                vectors, backend = self.embedding.embed([query])
                if self.embedding.last_failure or backend != self.index.spec.embedding_backend or len(vectors) != 1:
                    raise ValueError("embedding_backend_or_response_mismatch")
                dense = dict(self.index.search(vectors[0], keys, len(keys)))
            if mode == "lexical":
                scores = {key: value for key, value in lexical.items() if value > 0}
            elif mode == "dense":
                scores = dense
            else:
                scores = {}
                for ranking in (ranked({k: v for k, v in lexical.items() if v > 0}), ranked(dense)):
                    for rank, key in enumerate(ranking, 1):
                        scores[key] = scores.get(key, 0.0) + 1 / (self.rrf_k + rank)
            scale = max((abs(value) for value in scores.values()), default=1.0) or 1.0
            scores = {key: value + (scale * self.soft_boost if inferred_law is not None
                       and inferred_law in self.index.rows[key]["aliases"] else 0) for key, value in scores.items()}
            order = ranked(scores)
            if mode == "rerank":
                if self.reranker is None:
                    raise ValueError("reranker_unavailable")
                result["rerank_candidates"] = len(order)
                values = self.reranker.score(query, [self.index.rows[key]["text"] for key in order])
                if (self.reranker.last_failure or values is None or len(values) != len(order)
                        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values)):
                    raise ValueError("invalid_rerank_response")
                scores = dict(zip(order, values))
                order = sorted(order, key=lambda key: -scores[key])
            result.update(available=True, hits=[{**self.index.rows[key], "score": round(scores[key], 6) if mode == "lexical" else scores[key]} for key in order[:limit]])
        except Exception as exc:
            # Backend failures are data in experiments, never lexical fallbacks.
            result["failure"] = f"{type(exc).__name__}:{str(exc) if isinstance(exc, ValueError) else 'client_failure'}"
        result["latency_ms"] = (perf_counter() - started) * 1000
        return result
