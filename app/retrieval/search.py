"""Keyword page search over a case: query expansion, routing of the question and quote selection."""
from __future__ import annotations

import re
from typing import Any

from app.core.text import concise
from app.storage.db import connect, rowdict

SEMANTIC_GROUPS = [
    ["不知道", "不清楚", "不了解", "没听说", "不知情", "我以为合法", "认为是合法的"],
    ["固定回报", "保本保息", "年化收益", "承诺收益", "到期还本"],
    ["公开募集", "公开宣传", "不特定对象", "二维码转发", "社会公众"],
    ["资金流向", "银行流水", "转账", "入账", "出账", "账户"],
]

STOP_CHARS = set("的了是在与和或对把被从到这那有无是否什么哪些怎么为什么请帮我分析说明情况问题关于进行一个")


def expand_semantics(question: str) -> list[str]:
    expanded: list[str] = []
    for group in SEMANTIC_GROUPS:
        if any(term in question for term in group):
            expanded.extend(group)
    return list(dict.fromkeys(expanded))


def query_terms(question: str) -> list[str]:
    question = re.sub(r"[，。！？；：、,.!?;:\s()（）\[\]【】]", "", question)
    terms = re.findall(r"[A-Za-z0-9_.%-]{2,}|[\u4e00-\u9fff]{2,}", question)
    output: list[str] = []
    for term in terms:
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            chunks = [ch for ch in term if ch not in STOP_CHARS]
            joined = "".join(chunks)
            output.extend(joined[i : i + 2] for i in range(max(0, len(joined) - 1)))
            if len(joined) <= 8:
                output.append(joined)
        else:
            output.append(term)
    output.extend(expand_semantics(question))
    return [x for x in dict.fromkeys(output) if x]


def detect_route(question: str) -> str:
    # Database statistics can answer catalog totals, not amounts or durations
    # stated inside the case materials. A bare "多少" must not bypass retrieval.
    catalog_count = any(x in question for x in ("多少", "几份", "数量", "统计", "合计", "总共", "共有", "几页", "几条"))
    catalog_subject = any(x in question for x in (
        "卷宗", "文件", "文档", "材料", "文书", "页数", "页面", "证据事项", "证据条目", "目录", "档案",
    ))
    content_subject = any(x in question for x in (
        "金额", "付款", "资金", "期限", "利息", "刑期", "赔偿", "损失", "合同", "流水", "笔录",
    ))
    if catalog_count and catalog_subject and not content_subject:
        return "目录统计"
    if any(x in question for x in ["矛盾", "对比", "不一致", "分别", "差异"]):
        return "多文档对比"
    if any(x in question for x in ["法律", "法规", "构成要件", "规定", "法条"]):
        return "知识库+卷宗"
    return "事实检索"


def search_pages(case_id: int, question: str, limit: int = 8, *, allow_fallback: bool = True) -> list[dict[str, Any]]:
    terms = query_terms(question)
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT p.id AS page_id, p.page_no, p.text, p.summary,
                   d.id AS document_id, d.name, d.doc_type, d.people, d.date_range
            FROM pages p JOIN documents d ON d.id = p.document_id
            WHERE d.case_id = ?
            """,
            (case_id,),
        ).fetchall()
    finally:
        conn.close()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        item = rowdict(row)
        haystack = f"{item['name']} {item['doc_type']} {item['people']} {item['text']}".lower()
        matches = []
        score = 0.0
        for term in terms:
            count = haystack.count(term.lower())
            if count:
                score += count * max(1.0, len(term) ** 1.25)
                matches.append(term)
        if score:
            item["score"] = round(score, 2)
            item["matches"] = matches[:8]
            item["quote"] = best_quote(item["text"], matches)
            scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1]["document_id"], x[1]["page_no"]))
    if not scored and allow_fallback:
        fallback = [rowdict(x) for x in rows[:limit]]
        for item in fallback:
            item["score"] = 0
            item["matches"] = []
            item["quote"] = concise(item["text"], 180)
        return fallback
    return [item for _, item in scored[:limit]]


def best_quote(text: str, matches: list[str]) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    positions = [normalized.find(term) for term in matches if normalized.find(term) >= 0]
    pos = min(positions) if positions else 0
    start = max(0, pos - 45)
    end = min(len(normalized), pos + 155)
    prefix = "…" if start else ""
    suffix = "…" if end < len(normalized) else ""
    return prefix + normalized[start:end] + suffix


def statistics_answer(case_id: int, question: str) -> tuple[str, list[dict[str, Any]]]:
    conn = connect()
    try:
        doc_count = conn.execute("SELECT COUNT(*) FROM documents WHERE case_id = ?", (case_id,)).fetchone()[0]
        page_count = conn.execute(
            "SELECT COALESCE(SUM(pages), 0) FROM documents WHERE case_id = ?", (case_id,)
        ).fetchone()[0]
        evidence_count = conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id = ?", (case_id,)).fetchone()[0]
        types = conn.execute(
            "SELECT doc_type, COUNT(*) AS n FROM documents WHERE case_id = ? GROUP BY doc_type ORDER BY n DESC", (case_id,)
        ).fetchall()
        people_values = [x[0] for x in conn.execute("SELECT people FROM documents WHERE case_id = ? AND people <> ''", (case_id,))]
    finally:
        conn.close()
    people = []
    for value in people_values:
        for person in re.split(r"[、,，\s]+", value):
            if person and person not in people:
                people.append(person)
    breakdown = "、".join(f"{x['doc_type']} {x['n']}份" for x in types)
    answer = f"当前案件共有 **{doc_count} 份卷宗、{page_count} 页内容、{evidence_count} 条证据事项**。文书构成为：{breakdown or '暂无'}。"
    if any(x in question for x in ["人员", "几个人", "多少人"]):
        answer += f"\n\n目录中识别到 {len(people)} 名相关人员：{'、'.join(people) or '暂无'}。"
    citations = search_pages(case_id, question, 4)
    return answer, citations
