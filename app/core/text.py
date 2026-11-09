"""Small text helpers shared across domains."""
from __future__ import annotations

import re


def concise(text: str, limit: int = 180) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for marker in ("。", "；", ";", "！", "？"):
        pos = cut.rfind(marker)
        if pos > limit // 2:
            return cut[: pos + 1]
    return cut + "…"
