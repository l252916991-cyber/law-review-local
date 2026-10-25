"""Answer-level evaluation on a frozen case dataset.

This layer is deliberately separate from the page-level retrieval benchmark:

* retrieval metrics ask "did the right page come back";
* answer metrics ask "does the answer state the required facts, avoid claims the
  materials do not support, and cite acceptable pages".

Deterministic checks run offline and need no model. The optional judge is a
separate, versioned prompt: it is reported next to the deterministic scores and
never replaces them. A dataset built by the maintainer is labelled as such — it
is a regression fixture, not lawyer-annotated ground truth.
"""
from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any, Iterable


REFUSAL_MARKERS = ("资料不足", "无法核验", "未检索到", "材料中没有", "不足以", "无法判断", "需要补充")
_NORMALISE = re.compile(r"[\s,，、；;：:％%]+")


def normalise(text: str) -> str:
    """Case/width-insensitive comparison for Chinese numerical facts."""
    value = str(text or "")
    value = value.replace("，", ",").replace("％", "%")
    value = _NORMALISE.sub("", value)
    return value.lower()


def load_dataset(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_dataset(payload)
    return payload


def validate_dataset(payload: dict[str, Any]) -> None:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("数据集必须包含非空 items")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("题目必须包含非空字符串 id")
        if not isinstance(item.get("question"), str) or not item["question"].strip():
            raise ValueError(f"question 必须是非空字符串: {item['id']}")
        for field in ("id", "question", "must_include", "acceptable_pages", "expect_refusal"):
            if field not in item:
                raise ValueError(f"题目缺少字段 {field}: {item.get('id')}")
        if item["id"] in seen:
            raise ValueError(f"题目 id 重复: {item['id']}")
        seen.add(item["id"])
        if not isinstance(item["must_include"], list) or any(not isinstance(v, str) or not v for v in item["must_include"]):
            raise ValueError(f"must_include 必须是列表: {item['id']}")
        if not isinstance(item["expect_refusal"], bool):
            raise ValueError(f"expect_refusal 必须是布尔值: {item['id']}")
        if not isinstance(item["acceptable_pages"], list):
            raise ValueError(f"acceptable_pages 必须是列表: {item['id']}")
        for page in item["acceptable_pages"]:
            if not (isinstance(page, list) and len(page) == 2 and isinstance(page[0], str)
                    and type(page[1]) is int and page[1] > 0):
                raise ValueError(f"acceptable_pages 必须是 [文件名, 页号] 列表: {item['id']}")
        if item["expect_refusal"] and item["acceptable_pages"]:
            raise ValueError(f"拒答题不应声明可接受页: {item['id']}")
    if payload.get("version") == 2:
        validate_frozen_sources(payload)


def validate_frozen_sources(payload: dict[str, Any]) -> None:
    """Validate v2's independently frozen synthetic evidence, never answer quotes."""
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("v2 必须冻结非空 sources")
    digest = hashlib.sha256(json.dumps(sources, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
    if payload.get("source_sha256") != digest:
        raise ValueError("sources 摘要不匹配")
    pages = {}
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("sources 必须为页对象列表")
        name, page, text = source.get("document_name"), source.get("page"), source.get("text")
        if not isinstance(name, str) or type(page) is not int or page < 1 or not isinstance(text, str):
            raise ValueError("sources 页格式无效")
        key = (name, page)
        if key in pages or source.get("sha256") != hashlib.sha256(text.encode()).hexdigest():
            raise ValueError("来源重复或页摘要不匹配")
        pages[key] = text
    categories = {"normal", "contradiction", "insufficient", "irrelevant", "statute_missing", "injection"}
    for item in payload["items"]:
        if item.get("category") not in categories or not item.get("case_description"):
            raise ValueError(f"题目缺少类别或案情: {item['id']}")
        if not item["expect_refusal"] and not item["must_include"]:
            raise ValueError(f"可回答题必须包含必需要点: {item['id']}")
        if not item["expect_refusal"] and (not item["acceptable_pages"] or item.get("requires_citation") is not True):
            raise ValueError(f"可回答题必须要求来源引用: {item['id']}")
        pairs = [tuple(pair) for pair in item["acceptable_pages"]]
        if any(pair not in pages for pair in pairs):
            raise ValueError(f"可接受页不存在: {item['id']}")
        corpus = normalise("\n".join(pages[pair] for pair in pairs))
        if any(normalise(value) not in corpus for value in item["must_include"]):
            raise ValueError(f"必需要点没有来源支持: {item['id']}")


def _page_pairs(citations: Iterable[dict[str, Any]]) -> set[tuple[str, int]]:
    pairs: set[tuple[str, int]] = set()
    for citation in citations or []:
        name = citation.get("document_name") or citation.get("name")
        page = citation.get("page") or citation.get("page_no")
        if isinstance(name, str) and type(page) is int and page > 0:
            pairs.add((name, page))
    return pairs


def _refused(answer: str, answer_status: str | None) -> bool:
    if answer_status in {"insufficient_evidence", "clarification"}:
        return True
    return any(marker in (answer or "") for marker in REFUSAL_MARKERS)


def score_answer(item: dict[str, Any], answer: str, *, citations: list[dict[str, Any]] | None = None,
                 answer_status: str | None = None) -> dict[str, Any]:
    """Deterministic per-item score. Never calls a model."""
    text = normalise(answer)
    required = [str(value) for value in item.get("must_include") or []]
    matched = [value for value in required if normalise(value) in text]
    coverage = len(matched) / len(required) if required else 1.0

    forbidden = [str(value) for value in item.get("must_not_claim") or []]
    violations = [value for value in forbidden if normalise(value) in text]

    acceptable = {(name, page) for name, page in item.get("acceptable_pages") or []}
    cited = _page_pairs(citations or [])
    citation_precision = None
    if cited:
        citation_precision = round(len(cited & acceptable) / len(cited), 4) if acceptable else 0.0

    refused = _refused(answer, answer_status)
    refusal_ok = refused if item.get("expect_refusal") else True
    if item.get("expect_refusal"):
        passed = refusal_ok and not violations
    else:
        passed = coverage == 1.0 and not violations and (citation_precision is None or citation_precision == 1.0)
        if item.get("requires_citation") and not cited:
            passed = False

    return {
        "id": item["id"],
        "passed": passed,
        "coverage": round(coverage, 4),
        "matched": matched,
        "missing": [value for value in required if value not in matched],
        "violations": violations,
        "citation_precision": citation_precision,
        "refused": refused,
        "refusal_expected": bool(item.get("expect_refusal")),
    }


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        raise ValueError("没有可汇总的题目结果")
    answerable = [row for row in results if not row["refusal_expected"]]
    refusals = [row for row in results if row["refusal_expected"]]
    precisions = [row["citation_precision"] for row in answerable if row["citation_precision"] is not None]
    return {
        "queries": len(results),
        "passed": sum(1 for row in results if row["passed"]),
        "pass_rate": round(sum(1 for row in results if row["passed"]) / len(results), 4),
        "answerable_queries": len(answerable),
        "coverage_avg": round(sum(row["coverage"] for row in answerable) / len(answerable), 4) if answerable else None,
        "citation_precision_avg": round(sum(precisions) / len(precisions), 4) if precisions else None,
        "violation_count": sum(len(row["violations"]) for row in results),
        "refusal_queries": len(refusals),
        "refusal_correct": sum(1 for row in refusals if row["passed"]),
    }


def cohen_kappa(left: list[bool], right: list[bool]) -> float | None:
    """Agreement between a judge and a human on the same pass/fail labels."""
    if len(left) != len(right) or not left:
        raise ValueError("两组标签必须等长且非空")
    observed = sum(1 for a, b in zip(left, right) if a == b) / len(left)
    p_left = sum(1 for value in left if value) / len(left)
    p_right = sum(1 for value in right if value) / len(right)
    expected = p_left * p_right + (1 - p_left) * (1 - p_right)
    if expected == 1:
        return 1.0
    return round((observed - expected) / (1 - expected), 4)


JUDGE_PROMPT_VERSION = "answer-judge-v1"
JUDGE_INSTRUCTION = (
    "你是法律阅卷质量评审。仅根据给定的卷宗片段判断回答是否：(1) 覆盖必需要点；"
    "(2) 出现材料不支持的具体断言；(3) 引用页是否属于给定可接受页。"
    "只输出 JSON：{\"coverage_ok\":bool,\"unsupported_claims\":[string],\"citation_ok\":bool,\"rationale\":string}。"
    "不要给出法律意见，不要补充材料之外的事实。"
)


def judge_answer(item: dict[str, Any], answer: str, citations: list[dict[str, Any]], *,
                 call_model) -> dict[str, Any]:
    """Ask a model to review one answer. ``call_model`` must return raw text.

    The judge is an observation aid: its output is stored with its prompt
    version and must be compared against human labels before being trusted.
    """
    context = "\n".join(
        f"[资料{i}] {citation.get('document_name', '')} 第{citation.get('page', '')}页：{citation.get('quote', '')}"
        for i, citation in enumerate(citations or [], 1)
    ) or "（无引用片段）"
    prompt = (
        f"{JUDGE_INSTRUCTION}\n\n题目：{item['question']}\n"
        f"必需要点：{item.get('must_include') or '无'}\n"
        f"不得出现的断言：{item.get('must_not_claim') or '无'}\n"
        f"回答：{answer}\n\n引用片段：\n{context}"
    )
    raw = call_model(prompt)
    parsed = _extract_json(raw)
    return {
        "id": item["id"],
        "judge_version": JUDGE_PROMPT_VERSION,
        "raw": raw[:2000],
        "parsed": parsed,
        "available": parsed is not None,
    }


def _extract_json(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def evaluate_answers(dataset: dict[str, Any], answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Score canned answers keyed by item id; used by CI and offline runs."""
    missing = [item["id"] for item in dataset["items"] if item["id"] not in answers]
    if missing:
        raise ValueError(f"缺少答案：{', '.join(missing[:5])}")
    results = []
    for item in dataset["items"]:
        entry = answers[item["id"]]
        results.append(
            score_answer(
                item,
                entry.get("answer", ""),
                citations=entry.get("citations"),
                answer_status=entry.get("answer_status"),
            )
        )
    return {
        "dataset": dataset.get("name"),
        "version": dataset.get("version"),
        "notice": dataset.get("notice"),
        "summary": summarise(results),
        "per_item": results,
    }
