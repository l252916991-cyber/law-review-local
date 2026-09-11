"""Product-side statutory retrieval with a fail-closed citation boundary.

This module deliberately does not import benchmark task IDs. The corpus is an
immutable deployment artifact configured separately from case data.

A citation is only reported as ``ok`` when an explicitly named law and article
resolve to exactly one article of exactly one statute in the configured corpus.
Text search can never produce ``ok``: it only returns ``candidate``, because a
lexically similar passage is not a verified legal citation. An explicit
reference that cannot be resolved returns ``needs_review``; a nearby statute is
never substituted for the one the user named.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .legal_corpus import LegalCorpus, VersionAmbiguityError
from .statutory_gold import citations


LAW_CORPUS_ENV = "LAW_REVIEW_LEGAL_CORPUS_DIR"
CITATION_FIELDS = ("law_name", "version_date", "effective_date", "version_status",
                   "source_url", "document_id", "article_number", "subarticle_number",
                   "title", "text")

_corpus_cache: tuple[str, LegalCorpus] | None = None


def _configured_corpus() -> LegalCorpus | None:
    """Load the configured corpus once per directory; re-reads only when it changes."""
    global _corpus_cache
    directory = os.getenv(LAW_CORPUS_ENV, "").strip()
    if not directory:
        return None
    if _corpus_cache is not None and _corpus_cache[0] == directory:
        return _corpus_cache[1]
    corpus = LegalCorpus(Path(directory))
    _corpus_cache = (directory, corpus)
    return corpus


def corpus_status() -> dict[str, Any]:
    """Readiness for the configured corpus; never raises, so callers can report it."""
    directory = os.getenv(LAW_CORPUS_ENV, "").strip()
    if not directory:
        return {"configured": False, "available": False, "directory": None,
                "documents": 0, "error": "LAW_REVIEW_LEGAL_CORPUS_DIR is not set"}
    try:
        corpus = _configured_corpus()
    except (OSError, ValueError, KeyError) as exc:
        return {"configured": True, "available": False, "directory": directory,
                "documents": 0, "error": f"{type(exc).__name__}: {exc}"}
    assert corpus is not None
    return {"configured": True, "available": True, "directory": directory,
            "documents": len(corpus.documents), "error": None}


def parse_version(question: str) -> str | None:
    match = re.search(r"(?:19|20)\d{2}(?:年|[-/.])\d{1,2}(?:月|[-/.])?\d{0,2}", question)
    return match.group(0).replace("年", "-").replace("月", "-").rstrip("-") if match else None


def _public(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: hit.get(key) for key in CITATION_FIELDS} for hit in hits]


def _review(summary: str, warning: str, *, mode: str = "citation") -> dict[str, Any]:
    return {"status": "needs_review", "mode": mode, "summary": summary, "hits": [], "warning": warning}


def _resolve(corpus: LegalCorpus, refs: list[tuple[str, int]],
             version_date: str | None) -> tuple[list[dict[str, Any]], list[tuple[str, int]]]:
    """Return exact article hits plus any reference that is not uniquely resolvable."""
    hits: list[dict[str, Any]] = []
    unresolved: list[tuple[str, int]] = []
    for law_name, number in refs:
        found = corpus.lookup(law_name, number, version_date=version_date)
        # Two different statutes sharing a surface name, or no article at all, is
        # not a citation we may report.
        if not found or len({hit["law_name"] for hit in found}) > 1:
            unresolved.append((law_name, number))
            continue
        hits.extend(found)
    return hits, unresolved


def retrieve_statutory(question: str, *, limit: int = 5) -> dict[str, Any]:
    """Return verified citations, unverified candidates, or an explicit needs_review."""
    try:
        corpus = _configured_corpus()
    except (OSError, ValueError, KeyError) as exc:
        return _review("法条语料不可用或完整性校验失败", type(exc).__name__)
    if corpus is None:
        return _review("未配置法条语料，无法核验法律依据", "请律师核验法条版本与正文。")
    refs = citations(question)
    try:
        if refs:
            hits, unresolved = _resolve(corpus, refs, parse_version(question))
            if unresolved:
                named = "、".join(f"《{law}》第{number}条" for law, number in unresolved)
                return _review(f"无法唯一核验以下法条引用：{named}",
                               "未找到与引用精确对应的法条，不得以相近法条替代；请律师核验法名、条号与版本。")
            return {"status": "ok", "mode": "citation", "summary": f"精确核验到 {len(hits)} 条法条",
                    "hits": _public(hits), "warning": None}
        hits = _public(corpus.search(question, limit=limit))
        return {"status": "candidate" if hits else "needs_review", "mode": "discovery",
                "summary": f"检索到 {len(hits)} 条相关法条材料（未构成可核验引用）", "hits": hits,
                "warning": None if hits else "未检索到相关法条材料，请律师补充法名与条号。"}
    except VersionAmbiguityError as exc:
        return _review("法条存在多个版本，必须指定适用版本", str(exc))
    except (OSError, ValueError, KeyError) as exc:
        return _review("法条语料不可用或完整性校验失败", type(exc).__name__)
