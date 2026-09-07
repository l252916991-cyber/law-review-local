"""Product-side statutory retrieval with fail-closed version policy.

This module deliberately does not import benchmark task IDs. The corpus is an
immutable deployment artifact configured separately from case data.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .legal_corpus import LegalCorpus, VersionAmbiguityError


LAW_CORPUS_ENV = "LAW_REVIEW_LEGAL_CORPUS_DIR"


def _configured_corpus() -> LegalCorpus | None:
    directory = os.getenv(LAW_CORPUS_ENV, "").strip()
    if not directory:
        return None
    return LegalCorpus(Path(directory))


def parse_statutory_reference(question: str) -> tuple[str | None, int | None, str | None]:
    article = re.search(r"第([零〇一二三四五六七八九十百千万两0-9]+)条", question)
    number = None
    if article:
        raw = article.group(1)
        if raw.isdigit():
            number = int(raw)
        else:
            digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                      "六": 6, "七": 7, "八": 8, "九": 9}
            if raw == "十":
                number = 10
            elif "十" in raw:
                left, _, right = raw.partition("十")
                number = (digits.get(left, 1) if left else 1) * 10 + (digits.get(right, 0) if right else 0)
            elif all(ch in digits for ch in raw):
                number = sum(digits[ch] * (10 ** (len(raw) - i - 1)) for i, ch in enumerate(raw))
    law_name = None
    for candidate in ("刑法", "刑事诉讼法", "民法典", "公司法", "证券法", "行政处罚法", "劳动合同法"):
        if candidate in question:
            law_name = candidate
            break
    version_match = re.search(r"(?:19|20)\d{2}(?:年|[-/.])\d{1,2}(?:月|[-/.])?\d{0,2}", question)
    return law_name, number, version_match.group(0).replace("年", "-").replace("月", "-").rstrip("-") if version_match else None


def retrieve_statutory(question: str, *, limit: int = 5) -> dict[str, Any]:
    """Return citations or an explicit needs_review result; never guess versions."""
    law_name, article_number, version_date = parse_statutory_reference(question)
    corpus = _configured_corpus()
    if corpus is None:
        return {"status": "needs_review", "summary": "未配置法条语料，无法核验法律依据", "hits": [], "warning": "请律师核验法条版本与正文。"}
    try:
        if law_name and article_number is not None:
            hits = corpus.lookup(law_name, article_number, version_date=version_date)
        else:
            hits = corpus.search(question, law_name=law_name, version_date=version_date, limit=limit)
    except VersionAmbiguityError as exc:
        return {"status": "needs_review", "summary": "法条存在多个版本，必须指定适用版本", "hits": [], "warning": str(exc)}
    except (OSError, ValueError, KeyError) as exc:
        return {"status": "needs_review", "summary": "法条语料不可用或完整性校验失败", "hits": [], "warning": type(exc).__name__}
    citations = [
        {key: hit.get(key) for key in ("law_name", "version_date", "effective_date", "version_status", "source_url", "document_id", "article_number", "subarticle_number", "title", "text")}
        for hit in hits
    ]
    return {"status": "ok" if citations else "needs_review", "summary": f"检索到 {len(citations)} 条法条候选", "hits": citations,
            "warning": None if citations else "未检索到可核验法条，请律师补充法名、条号或版本。"}
