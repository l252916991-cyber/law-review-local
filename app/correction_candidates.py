"""Fixed-model correction candidates; no dataset or reference-answer access."""
from __future__ import annotations

import json
import re
from typing import Any

from app.benchmark_solver import _call, _configuration, solve
from app.benchmark_postprocess import preserve_correction_surface

MODES = ("baseline", "minimal", "phonetic", "patch", "detect", "review", "ensemble")
MINIMAL = "只修正确定的错别字、漏字、多字，优先最少字符修改。保留姓名、金额、日期、标点和排版，不润色，不扩写同义词。只返回完整修正句。"
PHONETIC = "逐字检查同音、近音、形近误字及漏字多字，结合上下文和法律常用搭配判断。只改必要字词，不改变原句事实及标点，直接输出完整修正句。"
PATCH = '只输出JSON数组，每项格式为{"old":"原文连续片段","new":"修正片段"}。old必须在原文仅出现一次，不同修改不能重叠；用足够上下文消除位置歧义。只修改确定的错别字、漏字、多字，不润色、不改标点。无错误输出[]。'


def apply_edits(origin: str, answer: str) -> str:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip())
    edits = json.loads(raw)
    if not isinstance(edits, list):
        raise ValueError("Edit response must be an array")
    spans = []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"old", "new"}:
            raise ValueError("Unexpected edit fields")
        old, new = edit["old"], edit["new"]
        if not isinstance(old, str) or not isinstance(new, str) or not old or origin.count(old) != 1:
            raise ValueError("Edit must match one source span")
        start = origin.index(old)
        spans.append((start, start + len(old), new))
    spans.sort()
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        raise ValueError("Overlapping edits")
    value = origin
    for start, end, new in reversed(spans):
        value = value[:start] + new + value[end:]
    return value


def predict(mode: str, instruction: str, question: str, config: dict[str, Any]) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError("Unknown correction mode")
    effective = _configuration({**config, "strategy": "task_guided"})
    calls: list[dict[str, Any]] = []

    def call(system: str, extra: str = "") -> str:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": instruction + "\n" + question}]
        if extra:
            messages.append({"role": "user", "content": extra})
        result = _call(messages, effective, mode)
        calls.append(result)
        if result["error"]:
            raise RuntimeError(result["error"])
        return result["prediction"]

    try:
        if mode == "baseline":
            result = solve("2-1", instruction, question, effective)
            calls.extend(result["calls"])
            if result["error"]:
                raise RuntimeError(result["error"])
            prediction = result["prediction"]
        elif mode in {"minimal", "phonetic"}:
            prediction = call(MINIMAL if mode == "minimal" else PHONETIC)
        elif mode == "patch":
            prediction = apply_edits(question, call(PATCH))
        elif mode == "detect":
            suspects = call("仅列出原句中可疑错字、漏字、多字的位置及原文片段；不重写句子。核对音近、形近及上下文。")
            prediction = call(MINIMAL, "以下只是待核对的疑点，可能误报；自行判断后输出最终句子：\n" + suspects)
        elif mode == "review":
            draft = call(MINIMAL)
            prediction = call(MINIMAL, "逐项复核下列草稿相对原句的修改，撤销不必要改写，检查漏改及新增错误，再输出最终句子。草稿不是标准答案：\n" + draft)
        else:
            first, second = call(MINIMAL), call(PHONETIC)
            prediction = call(MINIMAL, "以下两份独立草稿可能有误。根据原文核对每处差异，选择必要且最小的编辑，输出最终句子：\n候选甲：" + first + "\n候选乙：" + second)
        return {"prediction": preserve_correction_surface(question, prediction), "error": None, "calls": calls}
    except (ValueError, RuntimeError, TypeError) as exc:
        return {"prediction": "", "error": str(exc), "calls": calls}
