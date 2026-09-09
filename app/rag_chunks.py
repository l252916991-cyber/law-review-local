"""Deterministic page children with offsets into the unchanged source text."""
from __future__ import annotations

import re
from typing import Any

CHUNK_VERSION = "page-child-400-overlap50-v1"


def page_chunks(text: str, size: int = 400, overlap: int = 50) -> list[dict[str, Any]]:
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
