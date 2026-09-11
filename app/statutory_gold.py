"""Citation gold extracted from pinned LawBench question/answer text.

Statutory recall must be scored independently of the final answer score. Task 1-1
names its law and article in the question; tasks 3-1/3-2/3-8 name a law and one or
more articles inside the reference answer (``法条`` / ``法律依据``). This module
turns that surface text into ``(law_name, article_number)`` pairs; it never reads a
model prediction and never invents a citation the text does not contain.
"""
from __future__ import annotations

import re

from .legal_corpus import article_number

# NPC category labels that prefix a 1-1 question; 刑法 is excluded because it is
# both a category and a real statute name.
CATEGORIES = ("宪法相关法", "民法商法", "行政法", "经济法", "社会法", "诉讼与非诉讼程序法")
NUMBER = r"[零〇一二三四五六七八九十百千万两0-9]+"
STATUTE = r"[\u4e00-\u9fff]{1,25}?(?:法|条例|规定|解释|办法|细则|法典)"
LEADING = re.compile(r"^(?:根据|依据|依照|按照|参照)")
CATEGORY = re.compile(rf"^(?:{'|'.join(CATEGORIES)})")
# A category label can also leak mid-phrase as "<category>类中的<law>".
CATEGORY_LEAK = re.compile(r"^.*?类中的")
# Back-references to the statute under discussion ("依照本法第四十条") are not law
# names; resolving them to the current law is structured parsing, out of scope here.
# The 基 lookbehind keeps the real statute 基本法 intact.
SELF_REFERENCE = re.compile(r"(?<!基)本(?:法|条例|办法|规定|细则|规则)")
CITATION = re.compile(rf"《?({STATUTE})》?\s*第\s*({NUMBER})\s*条")


def citations(text: str) -> list[tuple[str, int]]:
    """Return unique ``(law_name, article_number)`` pairs in first-seen order."""
    result: list[tuple[str, int]] = []
    for match in CITATION.finditer(SELF_REFERENCE.sub("", text)):
        law = CATEGORY_LEAK.sub("", CATEGORY.sub("", LEADING.sub("", match.group(1)))).strip()
        if not law:
            continue
        pair = (law, article_number(match.group(2)))
        if pair not in result:
            result.append(pair)
    return result


def gold_citations(task_id: str, question: str, answer: str) -> list[tuple[str, int]]:
    """Task-aware source of truth: 1-1 states the citation, 3-* cite in the answer."""
    if task_id == "1-1":
        return citations(question)
    if task_id in {"3-1", "3-2", "3-8"}:
        return citations(answer)
    raise ValueError(f"No statutory gold definition for task {task_id}")
