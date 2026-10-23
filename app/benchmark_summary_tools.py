"""Gold-blind extractive helpers for the LawBench news-summary task."""
from __future__ import annotations

import re


TARGET_CHARACTERS = 120
MAX_SENTENCES = 4
_LEADING_BOILERPLATE = re.compile(
    r"^【[^】]*(?:免责声明|转载编辑|版权)[^】]*】\s*",
)
_TRAILING_MARKERS = ("原标题：", "责任编辑：", "值班主任：", "来源：")
_SENTENCE = re.compile(r".*?(?:[。！？!?]|$)")


def extractive_news_summary(question: str) -> str:
    """Return a bounded lead passage using the source text alone.

    The fixed policy keeps at most four complete lead sentences and stops once
    120 source characters are covered.  It removes only explicit publishing
    boilerplate, never paraphrases facts, and never reads a reference answer.
    """
    text = _LEADING_BOILERPLATE.sub("", question.strip())
    for marker in _TRAILING_MARKERS:
        position = text.find(marker)
        if position > 20:
            text = text[:position]
    sentences = [match.group(0).strip() for match in _SENTENCE.finditer(text) if match.group(0).strip()]
    if not sentences:
        return text
    selected: list[str] = []
    for sentence in sentences[:MAX_SENTENCES]:
        selected.append(sentence)
        if len("".join(selected)) >= TARGET_CHARACTERS:
            break
    return "".join(selected)
