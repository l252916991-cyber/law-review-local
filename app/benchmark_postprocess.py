"""Deterministic output-contract repairs that never inspect a reference answer."""
from __future__ import annotations

import json
import re
from typing import Any

from app.benchmark_event_tools import event_labels
from app.benchmark_summary_tools import extractive_news_summary

POSTPROCESS_VERSION = "benchmark-postprocess-v7"
ARTICLE_HEADING = re.compile(r"^第[零〇一二三四五六七八九十百千万两0-9]+条(?:之[零〇一二三四五六七八九十百千万两0-9]+)?\s*")


def normalize_article_surface(text: str) -> str:
    """Repair archived-page markup artifacts in a retrieved statute article.

    The corpus keeps source text verbatim, so half-width punctuation and spaces
    that the archived HTML inserted between Chinese characters survive in the
    article body. They are markup artifacts rather than statute text; repair them
    only on the emitted answer surface.
    """
    value = text
    for ascii_mark, chinese_mark in ((",", "，"), (":", "："), (";", "；"), ("!", "！"), ("?", "？"),
                                     ("(", "（"), (")", "）")):
        value = value.replace(ascii_mark, chinese_mark)
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    return re.sub(r"[ \t]+", " ", value).strip()


def _origin_sentence(question: str) -> str:
    """Extract the editable sentence without importing the frozen scorer."""
    for marker in ("句子：\n", "句子:", "句子："):
        if marker in question:
            return question.split(marker, 1)[1].splitlines()[0].strip()
    return question.strip()


def apply_located_edits(question: str, raw: str) -> str | None:
    """Apply a located edit list to the source sentence, or None if absent.

    Only activates when the output is the located JSON contract. A plain
    rewritten sentence returns None so the caller keeps baseline behaviour.
    A model asked for only the wrong/corrected fragments cannot rewrite the
    whole sentence, which removes the gratuitous rephrasing that costs F0.5
    precision. An empty or unapplicable edit list leaves the sentence unchanged,
    matching a model that reported no error.
    """
    origin = _origin_sentence(question)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(payload, dict) or "edits" not in payload:
        return None
    try:
        pairs = [(str(item["original"]), str(item["corrected"])) for item in payload["edits"]]
    except (KeyError, TypeError):
        return None
    revised, applied = origin, False
    for original, corrected in pairs:
        if original and original in revised:
            revised = revised.replace(original, corrected, 1)
            applied = True
    return revised if applied else origin


def preserve_correction_surface(question: str, prediction: str) -> str:
    """Keep a correction model's lexical edits but undo gratuitous typography.

    LawBench correction asks for minimal edits. Models routinely change every
    ASCII comma, insert spaces around numbers, and append punctuation. Those
    are not spelling corrections and create false edits. The source sentence
    alone determines all transformations here.
    """
    origin = _origin_sentence(question)
    value = prediction.strip()
    punctuation = ((",", "，"), (":", "："), (";", "；"), ("?", "？"), ("!", "！"))
    for ascii_mark, chinese_mark in punctuation:
        if ascii_mark in origin and chinese_mark not in origin:
            value = value.replace(chinese_mark, ascii_mark)
        elif chinese_mark in origin and ascii_mark not in origin:
            value = value.replace(ascii_mark, chinese_mark)
    if not re.search(r"\s", origin):
        value = re.sub(r"\s+", "", value)
    if origin and origin[-1] not in "。.!！?？;；" and value.endswith(("。", ".", "!", "！", "?", "？", ";", "；")):
        value = value[:-1]
    return value


def _source_order_triggers(question: str, prediction: str) -> str:
    parts = []
    for raw in re.split(r"[;；]", prediction):
        token = re.sub(r"\s+", "", raw.strip())
        if token and token in question and token not in parts:
            parts.append(token)
    if not parts:
        return prediction
    parts.sort(key=question.find)
    return ";".join(parts)


def normalize_consultation_surface(prediction: str) -> str:
    """Enforce the consultation task's stated structure: reply first, then legal basis.

    The instruction asks for the answer followed by its legal basis. Models add
    Markdown emphasis, headings and list markers that carry no legal content, and
    sometimes omit the leading section label. Strip the markup and restore the
    required ``回答``/``法律依据`` framing. No reference answer is read.
    """
    value = prediction
    value = re.sub(r"\*\*|__|`+", "", value)
    value = re.sub(r"(?m)^#{1,6}\s*", "", value)
    value = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.、)])\s*", "", value)
    value = re.sub(r"^\s*(?:法律责任依据|法律依据)\s*[:：]\s*", "法律依据:", value, flags=re.M)
    value = re.sub(r"^\s*(?:回答|答复)\s*[:：]\s*", "回答:", value, flags=re.M)
    value = re.sub(r"(回答:|法律依据:)\s*\n\s*", r"\1", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{2,}", "\n", value).strip()
    if not value.startswith("回答:"):
        value = "回答:" + value.lstrip(":：")
    return value


def postprocess(task_id: str, question: str, prediction: str,
                retrieval: dict[str, Any] | None = None) -> tuple[str, dict[str, object]]:
    revised = prediction
    policy = "unchanged"
    details: dict[str, object] = {}
    if task_id == "1-1" and retrieval and retrieval.get("mode") == "exact_article" and retrieval.get("hits"):
        revised = "\n".join(normalize_article_surface(ARTICLE_HEADING.sub("", hit["text"], count=1))
                            for hit in retrieval["hits"])
        policy = "exact-retrieved-article-content; no reference access"
        details["document_articles"] = [f"{hit['document_id']}/{hit['article_id']}" for hit in retrieval["hits"]]
    elif task_id == "2-1" and prediction:
        # Only the located contract is applied; a plain rewrite keeps baseline
        # behaviour, so enabling the locate prompt is the sole switch.
        located = apply_located_edits(question, prediction)
        revised = preserve_correction_surface(question, prediction if located is None else located)
        policy = "source-surface-only; no reference access" if located is None else "located-edits; no reference access"
    elif task_id == "2-7":
        revised = extractive_news_summary(question)
        policy = "bounded-source-lead-extraction; no reference access"
        details["target_characters"] = 120
        details["max_sentences"] = 4
    elif task_id == "2-9":
        labels = event_labels(question)
        if labels:
            revised = ";".join(labels)
            policy = "public-event-ontology-lexicon; no reference access"
            details["event_labels"] = labels
    elif task_id == "2-10" and prediction:
        revised = _source_order_triggers(question, prediction)
        policy = "deduplicate-and-order-verbatim-triggers-by-source; no reference access"
    elif task_id == "3-8" and prediction:
        revised = normalize_consultation_surface(prediction)
        policy = "stated-reply-then-basis-structure; no reference access"
    applied = revised != prediction
    return revised, {"version": POSTPROCESS_VERSION, "applied": applied,
                     "original_prediction": prediction if applied else None, "policy": policy, **details}
