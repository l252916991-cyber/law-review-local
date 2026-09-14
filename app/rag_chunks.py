"""Deterministic page children with offsets into the unchanged source text."""
from __future__ import annotations

import re
from typing import Any

CHUNK_VERSION = "page-child-400-overlap50-v1"
WHOLE_PAGE_CHUNK_VERSION = "page-whole-v1"
DEFAULT_CHUNK_PROFILE = "sentence-400"

# The ablation knob: both profiles run the identical exact-scan ranking in
# app.rag._retrieve_children. ``whole-page`` emits one child per page, so a
# sentence-400 vs whole-page comparison isolates the chunk window and nothing
# else. The versions must differ or the cache would serve 400-char rows as
# whole-page children.
CHUNK_PROFILES: dict[str, dict[str, Any]] = {
    "sentence-400": {"size": 400, "overlap": 50, "version": CHUNK_VERSION},
    "whole-page": {"size": None, "overlap": 0, "version": WHOLE_PAGE_CHUNK_VERSION},
}


def page_chunks(text: str, size: int | None = 400, overlap: int = 50) -> list[dict[str, Any]]:
    if size is None:
        if overlap != 0:
            raise ValueError("Whole-page chunking does not support overlap")
        return [{"char_start": 0, "char_end": len(text), "text": text}] if text.strip() else []
    if size < 2 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size or overlap")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundaries = list(re.finditer(r"[。！？；\n]", text[start + size // 2:end]))
            if boundaries:
                end = start + size // 2 + boundaries[-1].end()
        if text[start:end].strip():
            chunks.append({"char_start": start, "char_end": end, "text": text[start:end]})
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks
