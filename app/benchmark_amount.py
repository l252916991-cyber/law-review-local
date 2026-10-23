"""Reference-free arithmetic extraction for LawBench crime-amount questions."""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

AMOUNT = r"(\d+(?:\.\d+)?)\s*(万|千)?\s*余?元"
AMOUNT_RE = re.compile(AMOUNT)
MULTIPLIER = {"万": 10000.0, "千": 1000.0, "": 1.0, None: 1.0}
CRIME = re.compile(r"骗取|骗得|盗走|窃得|盗得|被盗|侵占|受贿|贪污|抢劫|敲诈")
EXCLUDED_BEFORE = re.compile(r"退赔|退赃|返还|发还|分得|花费|挥霍|罚金|赔偿|支出|损失|获利|获款|借款|查获")
EXCLUDED_AFTER = re.compile(r"价格卖|卖给|销赃")
GLOBAL_PATTERNS = (
    rf"(?:犯罪金额|犯罪总金额)[^。；\n\d]{{0,20}}(?:人民币)?{AMOUNT}",
    rf"(?:所盗财物|被盗财物|被盗物品|以上赃款、赃物|盗窃他人财物)[^。；\n]{{0,20}}?"
    rf"(?:共计|合计|总价值为|价值合计)(?:人民币)?\s*{AMOUNT}",
    rf"(?:共计骗取|共骗取)[^。；\n]{{0,30}}?(?:人民币)?\s*{AMOUNT}",
    rf"骗取[^。；\n]{{0,30}}?(?:现金|财物)(?:共计|合计)(?:人民币)?\s*{AMOUNT}",
    rf"合计(?:为)?人民币\s*{AMOUNT}",
)


def _value(groups: Sequence[str | None]) -> float:
    number, unit = groups[-2:]
    if number is None:
        raise ValueError("Amount capture is missing its numeric value")
    return float(number) * MULTIPLIER[unit]


def crime_amount(question: str) -> tuple[float | None, dict[str, Any]]:
    """Prefer explicit totals, otherwise sum crime-tied cash and valuations."""
    totals: list[float] = []
    for pattern in GLOBAL_PATTERNS:
        totals.extend(_value(match[-2:]) for match in re.findall(pattern, question))
    if totals:
        value = max(totals)
        return value, {"version": "crime-amount-v1", "method": "explicit_total", "components": totals}
    proceeds = re.findall(rf"实际得款(?:人民币)?\s*{AMOUNT}", question)
    if proceeds:
        value = _value(proceeds[-1][-2:])
        return value, {"version": "crime-amount-v1", "method": "actual_proceeds", "components": [value]}

    components: list[tuple[tuple[int, int], float]] = []
    for match in re.finditer(rf"(?:价值|价格)(?:合计|为)?(?:人民币)?\s*{AMOUNT}", question):
        nearby = question[max(0, match.start() - 25):match.end() + 15]
        if not re.search(r"销赃|出售|卖给|获款|所得价款", nearby):
            components.append((match.span(), _value(match.groups()[-2:])))
    offset = 0
    previous_crime = False
    for sentence in re.split(r"([。；\n])", question):
        relevant = bool(CRIME.search(sentence) or (previous_crime and re.search(r"现金|人民币|价值", sentence)))
        if not relevant:
            offset += len(sentence)
            if sentence not in {"。", "；", "\n"} and sentence.strip():
                previous_crime = False
            continue
        for match in AMOUNT_RE.finditer(sentence):
            span = (offset + match.start(), offset + match.end())
            if any(start <= span[0] < end for (start, end), _ in components):
                continue
            before = sentence[max(0, match.start() - 22):match.start()]
            after = sentence[match.end():match.end() + 12]
            if not EXCLUDED_BEFORE.search(before) and not EXCLUDED_AFTER.search(after):
                components.append((span, _value(match.groups())))
        offset += len(sentence)
        if sentence not in {"。", "；", "\n"} and sentence.strip():
            previous_crime = bool(CRIME.search(sentence))
    if not components:
        return None, {"version": "crime-amount-v1", "method": "no_confident_amount", "components": []}
    values = [value for _, value in sorted(components)]
    return sum(values), {"version": "crime-amount-v1", "method": "component_sum", "components": values}


def format_amount(value: float) -> str:
    formatted = str(int(value)) if value.is_integer() else format(value, ".12g")
    return f"[金额]{formatted}元<eoa>"
