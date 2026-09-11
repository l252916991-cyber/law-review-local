"""Opt-in network-free article index accepting precomputed vectors."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .legal_corpus import LegalCorpus, SCHEMA_VERSION


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class IndexSpec:
    embedding_model: str
    embedding_dim: int
    embedding_backend: str
    normalization: str = "l2"
    segmentation: str = "legal-corpus-article-v1"
    schema_version: str = SCHEMA_VERSION
    parser_version: str = "legal-corpus-v1"

    def __post_init__(self) -> None:
        if (not self.embedding_model or not self.embedding_backend or self.embedding_dim < 1
                or self.normalization not in {"l2", "none"}
                or self.segmentation != "legal-corpus-article-v1" or self.schema_version != SCHEMA_VERSION
                or not self.parser_version):
            raise ValueError("Unsupported index specification")


def vector_values(vector: list[float], spec: IndexSpec) -> tuple[float, ...]:
    if (len(vector) != spec.embedding_dim or any(isinstance(x, bool) or not isinstance(x, (int, float))
            or not math.isfinite(x) for x in vector)):
        raise ValueError("Invalid vector dimension or values")
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid vector norm")
    return tuple(x / norm if spec.normalization == "l2" else float(x) for x in vector)


class StatutoryIndex:
    """Validity sidecars require effective_from, effective_to and a source.

    Explicit null effective_to asserts an evidenced open interval. Publication
    dates and existing currentness_not_asserted labels never establish validity.
    """

    def __init__(self, corpus: LegalCorpus, spec: IndexSpec,
                 vectors: dict[str, list[float]] | None = None,
                 validity: dict[str, dict[str, Any]] | None = None) -> None:
        self.spec = spec
        self.validity = json.loads(json.dumps(validity or {}, allow_nan=False))
        documents = json.loads(json.dumps(corpus.documents, allow_nan=False))
        parser_sha = hashlib.sha256(Path(__file__).with_name("legal_corpus.py").read_bytes()).hexdigest()
        self.fingerprint = digest({"format": "statutory-index-v1", "manifest": corpus.manifest,
                                   "documents": documents, "spec": asdict(spec),
                                   "parser_sha256": parser_sha, "validity": self.validity})
        self.rows: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        for doc in documents:
            document_id = doc["document_id"]
            if document_id in self.documents:
                raise ValueError("Duplicate document ID")
            self.documents[document_id] = doc
            for article in doc["articles"]:
                key = f"{document_id}/{article['article_id']}"
                if key in self.rows:
                    raise ValueError("Duplicate article ID")
                self.rows[key] = {**LegalCorpus._result(doc, article), "id": key,
                                  "aliases": sorted(set([doc["law_name"], *doc["aliases"]]))}
        if set(self.validity) - set(self.documents):
            raise ValueError("Validity references unknown documents")
        if vectors is not None and set(vectors) != set(self.rows):
            raise ValueError("Vectors must exactly cover article IDs")
        self.vectors = {key: vector_values(value, spec) for key, value in (vectors or {}).items()}

    def search(self, vector: list[float], eligible_ids: list[str], limit: int = 48) -> list[tuple[str, float]]:
        """Cosine search over an explicitly authorized candidate set, without I/O."""
        query = vector_values(vector, self.spec)
        scores = [(key, sum(a * b for a, b in zip(query, self.vectors[key])) /
                   (math.hypot(*query) * math.hypot(*self.vectors[key]))) for key in eligible_ids]
        return sorted(scores, key=lambda pair: (-pair[1], pair[0]))[:max(0, limit)]

    def save(self, path: str | Path) -> None:
        payload = {"fingerprint": self.fingerprint,
                   "vectors": {key: list(value) for key, value in self.vectors.items()}}
        Path(path).write_text(json.dumps({"payload": payload, "sha256": digest(payload)},
                                        allow_nan=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path, corpus: LegalCorpus, spec: IndexSpec,
             validity: dict[str, dict[str, Any]] | None = None) -> StatutoryIndex:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        payload = envelope["payload"]
        index = cls(corpus, spec, validity=validity)
        if envelope["sha256"] != digest(payload) or payload["fingerprint"] != index.fingerprint:
            raise ValueError("Statutory index cache mismatch")
        vectors = payload["vectors"]
        if vectors and set(vectors) != set(index.rows):
            raise ValueError("Cached vectors do not cover article IDs")
        index.vectors = {key: vector_values(value, spec) for key, value in vectors.items()}
        return index

    def eligible(self, *, explicit_law: str | None = None, explicit_laws: Iterable[str] | None = None,
                 query_effective_date: str | None = None,
                 today: date | None = None) -> tuple[list[str], list[str]]:
        """Candidate article IDs. ``explicit_laws`` mirrors a multi-law production scope."""
        if explicit_law is not None and explicit_laws is not None:
            raise ValueError("Pass explicit_law or explicit_laws, not both")
        allowed = {explicit_law} if explicit_law is not None else (set(explicit_laws) if explicit_laws is not None else None)
        target = date.fromisoformat(query_effective_date) if query_effective_date else (today or date.today())
        active: dict[str, list[str]] = {}
        issues: list[str] = []
        for key, doc in self.documents.items():
            if allowed is not None and not allowed & {doc["law_name"], *doc["aliases"]}:
                continue
            try:
                meta = self.validity[key]
                if not isinstance(meta["source"], str) or not meta["source"].strip():
                    raise ValueError("missing source")
                start = date.fromisoformat(meta["effective_from"])
                end = date.fromisoformat(meta["effective_to"]) if meta["effective_to"] is not None else None
                if end is not None and end <= start:
                    raise ValueError("invalid interval")
                if doc.get("effective_date") and date.fromisoformat(doc["effective_date"]) != start:
                    raise ValueError("conflicting effective date")
            except (KeyError, TypeError, ValueError):
                issues.append(f"unverified_validity:{key}")
                continue
            if start <= target and (end is None or target < end):
                active.setdefault(doc["law_name"], []).append(key)
        selected: set[str] = set()
        for law, keys in active.items():
            if len(keys) > 1:
                issues.append(f"ambiguous_versions:{law}")
            else:
                selected.update(keys)
        return [key for key, row in self.rows.items() if row["document_id"] in selected], issues
