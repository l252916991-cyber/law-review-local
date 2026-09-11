"""Framework-independent multi-agent runtime for grounded case review.

The runtime models an explicit DAG instead of hiding control flow in a prompt:
Planner -> Hybrid Retrieval -> parallel specialists -> Critic -> Memory.
Every node persists its input/output/status/latency for inspection and replay.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any, Callable

from .db import connect, now, transaction
from .rag import HybridRetriever, recall_memories, remember
from .services import call_local_llm, concise, detect_route, rowdict
from .statutory_retrieval import retrieve_statutory


logger = logging.getLogger(__name__)


def failure_diagnostic(exc: Exception, phase: str) -> dict[str, str]:
    """Expose useful categories, never exception bodies containing case data."""
    return {"phase": phase, "error_type": type(exc).__name__, "code": f"{phase}_failed"}


def answer_contract(answer: str, citation_count: int) -> dict[str, bool]:
    """Syntactic contract only; this does not establish citation entailment."""
    answer = answer or ""
    tokens = re.findall(r"[\[【［]\s*资料[^\]】］\n]*(?:[\]】］]|$)", answer)
    parsed = [re.fullmatch(r"\[\s*资料\s*(\d+)\s*\]", token) for token in tokens]
    markers = [int(match.group(1)) for match in parsed if match]
    return {
        "non_empty": bool(answer.strip()),
        "citations_valid": bool(markers) and all(parsed)
        and all(1 <= value <= citation_count for value in markers),
        "lawyer_review_present": bool(re.search(
            r"律师.{0,24}(?:复核|核验|核对|回看|审阅)|(?:请|需|须|待).{0,8}(?:复核|核验)", answer
        )) and not bool(re.search(
            r"(?:无需|不必|不用|不需要).{0,8}(?:律师)?.{0,8}(?:复核|核验)|律师.{0,8}(?:无需|不必|不用|不需要).{0,8}(?:复核|核验)", answer
        )),
    }


def validate_review_answer(answer: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    checks = answer_contract(answer, len(contexts))
    markers = [int(value) for value in re.findall(r"\[\s*资料\s*(\d+)\s*\]", answer or "")]
    checks["sources_valid"] = bool(markers) and all(
        1 <= index <= len(contexts)
        and isinstance(contexts[index - 1].get("document_id"), int)
        and contexts[index - 1]["document_id"] > 0
        and isinstance(contexts[index - 1].get("page_no"), int)
        and contexts[index - 1]["page_no"] > 0
        and bool(str(contexts[index - 1].get("quote") or "").strip())
        for index in markers
    )
    return {
        "valid": all(checks.values()), "checks": checks,
        "issues": [name for name, passed in checks.items() if not passed],
        "scope": "format_range_source_presence_and_review_reminder",
        "semantic_entailment_checked": False,
        "amount_calculations_checked": False,
        "lawyer_review_required": True,
    }


def _citation(item: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "document_id": item["document_id"],
        "document_name": item["name"],
        "page": item["page_no"],
        "quote": item["quote"],
        "retrieval_explain": item.get("retrieval_explain", ""),
        "channels": item.get("channels", []),
        "url": f"/api/documents/{item['document_id']}/file#page={item['page_no']}",
    }


@dataclass(frozen=True)
class PlanNode:
    name: str
    role: str
    depends_on: tuple[str, ...]
    objective: str
    input_scope: str = "case_context"
    output_schema: str = "specialist-v1"


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    role: str
    input_scope: str
    output_schema: str
    depends_on: tuple[str, ...]
    runner: Callable[[str, list[dict[str, Any]]], dict[str, Any]]


def validate_specialist_output(
    output: dict[str, Any], contexts: list[dict[str, Any]], *, schema: str = "specialist-v1"
) -> dict[str, Any]:
    """Validate specialist structure and bound source indexes to retrieval context."""
    if not isinstance(output, dict):
        raise ValueError("specialist output must be an object")
    normalized = dict(output)
    normalized.setdefault("schema_version", schema)
    normalized.setdefault("status", "ok")
    normalized.setdefault("summary", "")
    if normalized["status"] not in {"ok", "needs_review", "failed"}:
        raise ValueError("invalid specialist status")
    max_index = len(contexts)
    for key in ("facts", "sources", "conflicts", "gaps", "items"):
        values = normalized.get(key)
        if values is None:
            continue
        if not isinstance(values, list):
            raise ValueError(f"specialist field {key} must be a list")
        for item in values:
            if not isinstance(item, dict):
                raise ValueError(f"specialist field {key} contains a non-object")
            source_index = item.get("source_index", item.get("index"))
            if source_index is not None and (type(source_index) is not int or not 1 <= source_index <= max_index):
                raise ValueError(f"specialist source index out of bounds: {source_index}")
    return normalized


def specialist_specs(coordinator: Any) -> dict[str, SubAgentSpec]:
    return {
        "facts": SubAgentSpec("facts", "事实 Agent", "all_case_pages", "facts-v1", ("retrieve",), coordinator.fact_agent.run),
        "evidence": SubAgentSpec("evidence", "证据 Agent", "case_pages_and_evidence_catalog", "evidence-v1", ("retrieve",), coordinator.evidence_agent.run),
        "contradiction": SubAgentSpec("contradiction", "矛盾 Agent", "case_pages_and_evidence_catalog", "contradiction-v1", ("retrieve",), coordinator.contradiction_agent.run),
        "gap_detection": SubAgentSpec("gap_detection", "疏漏 Agent", "case_pages_and_evidence_catalog", "gap-v1", ("retrieve", "facts", "evidence"), coordinator.gap_detection_agent.run),
        "statutory_conflict": SubAgentSpec("statutory_conflict", "法条核验 Agent", "legal_corpus", "statutory-v1", ("retrieve",), lambda question, contexts: StatutoryConflictAgent().run(question, contexts)),
    }


class PlannerAgent:
    def build_plan(self, question: str) -> tuple[str, list[PlanNode]]:
        route = detect_route(question)
        nodes = [
            PlanNode("retrieve", "检索 Agent", (), "执行 BM25/向量双路召回与 RRF 融合"),
            PlanNode("facts", "事实 Agent", ("retrieve",), "提炼受来源约束的事实时间线"),
            PlanNode("evidence", "证据 Agent", ("retrieve",), "评估证据类型、独立来源与证明力"),
        ]
        if route == "多文档对比" or any(term in question for term in ("口供", "陈述", "审批")):
            nodes.append(PlanNode("contradiction", "矛盾 Agent", ("retrieve",), "跨文件比对否认、确认与客观记录"))

        if any(term in question for term in ("法律", "法规", "构成要件", "规定", "法条")):
            nodes.append(PlanNode("statutory_conflict", "法条核验 Agent", ("retrieve",), "检索指定版本法条并提示证据与法条核验事项", "legal_corpus", "statutory-v1"))

        # 自动添加疏漏检测节点
        if any(term in question for term in ("疏漏", "完整性", "缺失", "遗漏", "缺少", "待补")):
            nodes.append(PlanNode("gap_detection", "疏漏 Agent", ("retrieve", "facts", "evidence"), "检测证据链疏漏与待补证事项"))

        nodes.extend(
            [
                PlanNode(
                    "critic",
                    "审校 Agent",
                    tuple(node.name for node in nodes if node.name != "retrieve"),
                    "合并专家结果、消解冲突并检查引用覆盖",
                ),
                PlanNode("memory", "记忆 Agent", ("critic",), "沉淀可语义召回的案件长期记忆"),
            ]
        )
        return route, nodes


class RetrievalAgent:
    def __init__(self, case_id: int, prefer_remote_embeddings: bool = True):
        self.case_id = case_id
        self.prefer_remote_embeddings = prefer_remote_embeddings
        self.retriever = HybridRetriever(case_id, prefer_remote_embeddings)

    def retrieve(self, query: str, limit: int = 6) -> dict[str, Any]:
        from .agent_tools import execute_readonly_tool

        result = execute_readonly_tool(
            self.case_id,
            "search",
            {
                "query": query,
                "limit": limit,
            },
            use_remote_embeddings=self.prefer_remote_embeddings,
        )
        return {
            "contexts": result["items"],
            "metrics": result["retrieval_metrics"],
            "tool": result["tool"],
            "data_notice": result["data_notice"],
        }

    def retrieve_evidence(self, query: str, method: str = "hybrid") -> str:
        if method == "keyword":
            contexts = self.retriever.keyword_search(query, 6)
        elif method == "semantic":
            contexts = self.retriever.vector_search(query, 6)
        else:
            contexts, _ = self.retriever.retrieve(query, 6)
        if not contexts:
            return "未检索到相关证据材料"
        return "\n\n".join(
            f"[证据{i}] {item['name']} 第{item['page_no']}页：{item.get('quote') or concise(item['text'], 200)}"
            for i, item in enumerate(contexts, 1)
        )


class FactAgent:
    role = "事实 Agent"

    def run(self, question: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        facts = []
        for index, item in enumerate(contexts[:5], 1):
            facts.append(
                {
                    "statement": concise(item["quote"], 180),
                    "source": f"资料{index}",
                    "document": item["name"],
                    "page": item["page_no"],
                }
            )
        return {"summary": f"提炼 {len(facts)} 条可溯源事实", "facts": facts}


class EvidenceAgent:
    role = "证据 Agent"

    def run(self, question: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        type_weights = {"银行流水": 0.95, "电子数据": 0.88, "审计报告": 0.9, "询问笔录": 0.68}
        sources = []
        for index, item in enumerate(contexts, 1):
            sources.append(
                {
                    "source": f"资料{index}",
                    "type": item.get("doc_type", "其他材料"),
                    "independence": "客观记录" if item.get("doc_type") in {"银行流水", "电子数据", "审计报告"} else "言词/书面材料",
                    "initial_weight": type_weights.get(item.get("doc_type", ""), 0.62),
                }
            )
        objective = sum(1 for item in sources if item["independence"] == "客观记录")
        return {
            "summary": f"{len(sources)} 个来源中包含 {objective} 个客观记录来源",
            "sources": sources,
            "warning": "权重仅用于检索排序解释，不替代律师对证据能力与证明力的判断。",
        }


class ContradictionAgent:
    role = "矛盾 Agent"
    NEGATIONS = ("不知道", "不清楚", "没有", "未参与", "没看到", "否认")
    ASSERTIONS = ("确认", "执行", "要求", "回复", "决定", "审批", "固定回报")

    def run(self, question: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        denials = []
        assertions = []
        for index, item in enumerate(contexts, 1):
            text = item["quote"]
            if any(term in text for term in self.NEGATIONS):
                denials.append((index, item))
            if any(term in text for term in self.ASSERTIONS):
                assertions.append((index, item))
        conflicts = []
        for left_index, left in denials:
            for right_index, right in assertions:
                if left["document_id"] == right["document_id"] and left["page_no"] == right["page_no"]:
                    continue
                conflicts.append(
                    {
                        "denial": f"资料{left_index}：{concise(left['quote'], 130)}",
                        "counter": f"资料{right_index}：{concise(right['quote'], 130)}",
                        "verification": "核对形成时间、陈述主体、原始载体及上下文。",
                    }
                )
                if len(conflicts) >= 3:
                    break
            if len(conflicts) >= 3:
                break
        return {
            "summary": f"识别 {len(conflicts)} 组候选矛盾，需由律师回看原页确认",
            "conflicts": conflicts,
        }


class StatutoryConflictAgent:
    role = "法条核验 Agent"

    def run(self, question: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        result = retrieve_statutory(question)
        items = [
            {"law_name": hit.get("law_name"), "article_number": hit.get("article_number"),
             "version_date": hit.get("version_date"), "source_url": hit.get("source_url"),
             "text": concise(hit.get("text", ""), 400)}
            for hit in result["hits"]
        ]
        return {"status": result["status"], "summary": result["summary"], "statutes": items,
                "warning": result.get("warning"), "needs_lawyer_review": result["status"] != "ok"}


class AnalysisAgent:
    """Compatibility facade exposing two analysis operations as tools."""

    def __init__(self, case_id: int):
        self.case_id = case_id

    def analyze_contradiction(self, evidence_list: str) -> str:
        lines = [line for line in evidence_list.splitlines() if line.strip()]
        negatives = [line for line in lines if any(term in line for term in ContradictionAgent.NEGATIONS)]
        positives = [line for line in lines if any(term in line for term in ContradictionAgent.ASSERTIONS)]
        if negatives and positives:
            return f"候选矛盾：{negatives[0]} ↔ {positives[0]}\n待质证：核验原页、时间与载体。"
        return "未形成足够的跨来源矛盾对，请补充材料。"

    def summarize_case_facts(self, evidence_list: str) -> str:
        lines = [line.strip() for line in evidence_list.splitlines() if line.strip()]
        return "## 已检索事实\n" + "\n".join(f"- {line}" for line in lines[:5])


class CrossExaminationAgent:
    def __init__(self, case_id: int, prefer_remote_embeddings: bool = True):
        self.retrieval_agent = RetrievalAgent(case_id, prefer_remote_embeddings)

    def cross_examine(self, statement: str, person: str) -> dict[str, Any]:
        statements = self.retrieval_agent.retrieve_evidence(f"{person} 陈述 笔录 供述", "hybrid")
        corroboration = self.retrieval_agent.retrieve_evidence(f"{statement} 流水 邮件 合同", "hybrid")
        return {
            "all_statements": statements,
            "corroborating_evidence": corroboration,
            "focus_statement": statement,
        }


class GapDetectionAgent:
    """证据疏漏检测 Agent

    自动检测证据链中的疏漏、缺陷和待补证事项。
    """
    role = "疏漏 Agent"

    # 不同案件类型所需的证据类型
    EVIDENCE_REQUIREMENTS = {
        "金融犯罪": ["银行流水", "审计报告", "询问笔录", "电子数据"],
        "刑事": ["询问笔录", "物证", "书证"],
        "经济纠纷": ["合同", "银行流水", "书证"],
    }

    def __init__(self, case_id: int):
        self.case_id = case_id

    def run(self, question: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        """
        检测证据链疏漏

        返回:
            {
                "summary": str,
                "gaps": list[dict],
                "severity_distribution": dict
            }
        """
        gaps = []

        # 1. 检测证据类型缺失
        gaps.extend(self._detect_missing_evidence_types(contexts))

        # 2. 检测时间线缺口
        gaps.extend(self._detect_timeline_gaps(contexts))

        # 3. 检测逻辑链断点
        gaps.extend(self._detect_logic_gaps(contexts))

        # 4. 检测单一来源证据
        gaps.extend(self._detect_single_source_evidence(contexts))

        # 5. 检测金额不匹配
        gaps.extend(self._detect_amount_mismatches(contexts))

        severity_dist = {
            "high": len([g for g in gaps if g["severity"] == "高"]),
            "medium": len([g for g in gaps if g["severity"] == "中"]),
            "low": len([g for g in gaps if g["severity"] == "低"]),
        }

        return {
            "summary": f"识别 {len(gaps)} 处证据疏漏或待补证事项",
            "gaps": gaps,
            "severity_distribution": severity_dist
        }

    def _detect_missing_evidence_types(self, contexts: list[dict]) -> list[dict]:
        """检测缺失的证据类型"""
        gaps = []

        # 统计现有证据类型
        present_types = set(item.get("doc_type", "其他材料") for item in contexts)

        # 获取案件类型（从数据库查询）
        conn = connect()
        try:
            case_type = conn.execute(
                "SELECT case_type FROM cases WHERE id=?", (self.case_id,)
            ).fetchone()
            case_type = case_type["case_type"] if case_type else "刑事"
        finally:
            conn.close()

        # 检查必需证据
        required = self.EVIDENCE_REQUIREMENTS.get(case_type, self.EVIDENCE_REQUIREMENTS["刑事"])
        missing = [t for t in required if t not in present_types]

        if missing:
            gaps.append({
                "type": "证据类型缺失",
                "severity": "高" if len(missing) >= 2 else "中",
                "description": f"缺少关键证据类型：{', '.join(missing)}",
                "suggestion": f"建议补充{', '.join(missing)}以支撑案件事实",
                "affected_evidence_ids": []
            })

        return gaps

    def _detect_timeline_gaps(self, contexts: list[dict]) -> list[dict]:
        """检测时间线缺口（超过30天空白期）"""
        gaps = []

        # 提取所有日期范围
        import re
        from datetime import datetime

        dates = []
        for item in contexts:
            date_range = item.get("date_range", "")
            if not date_range:
                continue

            match = re.search(r"(\d{4})-(\d{2})", date_range)
            if match:
                try:
                    date = datetime.strptime(f"{match.group(1)}-{match.group(2)}-01", "%Y-%m-%d")
                    dates.append(date)
                except ValueError:
                    continue

        if len(dates) < 2:
            return gaps

        dates.sort()

        # 检查相邻日期间隔
        for i in range(len(dates) - 1):
            delta_days = (dates[i + 1] - dates[i]).days
            if delta_days > 60:  # 超过60天认为是较大缺口
                gaps.append({
                    "type": "时间线断裂",
                    "severity": "中",
                    "description": f"{dates[i].strftime('%Y-%m')} 至 {dates[i+1].strftime('%Y-%m')} 之间存在 {delta_days} 天空白期",
                    "suggestion": "核实该时间段是否有关键事件或证据遗漏",
                    "affected_evidence_ids": []
                })

        return gaps

    def _detect_logic_gaps(self, contexts: list[dict]) -> list[dict]:
        """检测逻辑链断点（当事人陈述缺少客观证据印证）"""
        gaps = []

        # 检查是否有否认陈述
        denials = [
            item for item in contexts
            if any(term in item.get("text", "") for term in ContradictionAgent.NEGATIONS)
        ]

        # 检查是否有客观证据
        objective_types = {"银行流水", "电子数据", "审计报告", "鉴定意见"}
        objective_evidence = [
            item for item in contexts
            if item.get("doc_type") in objective_types
        ]

        if denials and not objective_evidence:
            gaps.append({
                "type": "逻辑链断点",
                "severity": "高",
                "description": "存在当事人否认陈述，但缺少客观证据印证",
                "suggestion": "需补充银行流水、电子邮件、通话记录等客观记录",
                "affected_evidence_ids": []
            })

        return gaps

    def _detect_single_source_evidence(self, contexts: list[dict]) -> list[dict]:
        """检测单一来源证据（可能需要印证）"""
        gaps = []

        # 统计各类型证据的数量
        from collections import defaultdict
        type_counts = defaultdict(int)
        for item in contexts:
            doc_type = item.get("doc_type", "其他材料")
            type_counts[doc_type] += 1

        # 关键证据类型只有一份时提示
        critical_types = {"询问笔录", "银行流水", "合同"}
        for doc_type in critical_types:
            if doc_type in type_counts and type_counts[doc_type] == 1:
                gaps.append({
                    "type": "单一来源",
                    "severity": "低",
                    "description": f"{doc_type}仅有一份，可能需要其他证据印证",
                    "suggestion": "核实是否有其他相关证据可补充",
                    "affected_evidence_ids": []
                })

        return gaps

    def _detect_amount_mismatches(self, contexts: list[dict]) -> list[dict]:
        """检测金额不匹配"""
        gaps = []

        import re

        # 提取所有金额
        amounts = []
        for item in contexts:
            text = item.get("text", "")
            # 匹配形如 "2,860万元" "28,600,000元" 的金额
            matches = re.findall(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*万?\s*元", text)
            for match in matches:
                try:
                    value = float(match.replace(",", ""))
                    amounts.append({
                        "value": value,
                        "text": match,
                        "doc_type": item.get("doc_type"),
                        "source": item.get("name")
                    })
                except ValueError:
                    continue

        # 简单检查：如果笔录和流水中的金额差异较大
        if len(amounts) >= 2:
            transcript_amounts = [a for a in amounts if "笔录" in a.get("doc_type", "")]
            flow_amounts = [a for a in amounts if "流水" in a.get("doc_type", "")]

            if transcript_amounts and flow_amounts:
                # 检查是否有显著差异（示例逻辑，实际需要更精细）
                gaps.append({
                    "type": "金额核对",
                    "severity": "中",
                    "description": "笔录与银行流水中均提及金额，建议核对一致性",
                    "suggestion": "逐笔比对笔录陈述金额与银行流水实际金额",
                    "affected_evidence_ids": []
                })

        return gaps


class CriticAgent:
    role = "审校 Agent"

    def __init__(self):
        from .config import LLMConfig

        config = LLMConfig.from_env()
        self.model = os.getenv("LAW_REVIEW_AGENT_CRITIC_MODEL", config.model)
        self.timeout = int(os.getenv("LAW_REVIEW_AGENT_CRITIC_TIMEOUT", str(config.timeout)))

    def run(
        self,
        question: str,
        route: str,
        contexts: list[dict[str, Any]],
        specialist_outputs: dict[str, dict[str, Any]],
        use_llm: bool,
    ) -> dict[str, Any]:
        failed_validation = None
        diagnostic = None
        if use_llm:
            expert_summary = "；".join(specialist_outputs[name].get("summary", "") for name in sorted(specialist_outputs))
            grounded_question = f"{question}\n专家节点摘要：{expert_summary}。请只输出给律师的最终结论。"
            try:
                answer = call_local_llm(
                    grounded_question,
                    f"Multi-Agent/{route}",
                    contexts,
                    timeout=self.timeout,
                    model_override=self.model,
                )
                validation = validate_review_answer(answer, contexts)
                if validation["valid"]:
                    return {
                        "answer": answer, "llm_used": True, "llm_model": self.model,
                        "fallback_reason": None, "citation_check": "passed",
                        "validation": validation, "memory_eligible": True,
                        "llm_attempted": True, "failure_diagnostic": None,
                    }
                failed_validation = validation
                failure = "模型输出未通过格式/来源校验：" + ", ".join(validation["issues"])
                diagnostic = {"phase": "critic_validation", "code": "invalid_answer_contract"}
                logger.warning("critic_validation_failed issues=%s", ",".join(validation["issues"]))
            except (RuntimeError, OSError, ValueError, TypeError) as exc:
                diagnostic = failure_diagnostic(exc, "critic_llm")
                logger.warning("critic_llm_failed error_type=%s", type(exc).__name__)
                failure = f"本地模型不可用或响应异常（{self.timeout} 秒总时限，{type(exc).__name__}）"
        else:
            failure = "本次关闭 LLM，由审校节点执行规则汇总"

        fact_lines = [
            f"- {item['statement']} [{item['source']}]"
            for item in specialist_outputs.get("facts", {}).get("facts", [])[:4]
        ]
        conflict_lines = []
        for item in specialist_outputs.get("contradiction", {}).get("conflicts", [])[:2]:
            conflict_lines.append(f"- {item['denial']}；对照 {item['counter']}")

        # 添加疏漏检测结果
        gap_lines = []
        for gap in specialist_outputs.get("gap_detection", {}).get("gaps", [])[:3]:
            severity_icon = {"高": "🔴", "中": "🟡", "低": "🟢"}.get(gap["severity"], "⚪")
            gap_lines.append(f"- {severity_icon} **{gap['type']}**: {gap['description']}")

        answer = "## 可核验事实\n" + ("\n".join(fact_lines) or "- 当前材料不足")
        if conflict_lines:
            answer += "\n\n## 候选矛盾\n" + "\n".join(conflict_lines)
        if gap_lines:
            answer += "\n\n## 证据疏漏检测\n" + "\n".join(gap_lines)
        answer += f"\n\n> 审校说明：{failure}；结论必须由律师点击资料卡回看原页。仅检查引用格式和来源存在性，未验证语义支持或金额计算。"
        validation = validate_review_answer(answer, contexts)
        return {
            "answer": answer,
            "llm_used": False,
            "llm_model": self.model,
            "fallback_reason": failure,
            "citation_check": "passed" if validation["valid"] else "failed",
            "validation": validation,
            "rejected_llm_validation": failed_validation,
            "failure_diagnostic": diagnostic,
            "llm_attempted": use_llm,
            "memory_eligible": validation["valid"] and not use_llm,
        }


class LawReviewCoordinator:
    def __init__(self, case_id: int, prefer_remote_embeddings: bool = True):
        self.case_id = case_id
        self.prefer_remote_embeddings = prefer_remote_embeddings
        self.planner_agent = PlannerAgent()
        self.retrieval_agent = RetrievalAgent(case_id, prefer_remote_embeddings)
        self.fact_agent = FactAgent()
        self.evidence_agent = EvidenceAgent()
        self.contradiction_agent = ContradictionAgent()
        self.gap_detection_agent = GapDetectionAgent(case_id)
        self.analysis_agent = AnalysisAgent(case_id)
        self.cross_exam_agent = CrossExaminationAgent(case_id, prefer_remote_embeddings)
        self.critic_agent = CriticAgent()

    def _start_run(self, question: str, route: str) -> int:
        with transaction() as conn:
            return conn.execute(
                """INSERT INTO agent_runs(case_id, question, route, status, runtime, created_at)
                   VALUES (?, ?, ?, 'running', 'native', ?)""",
                (self.case_id, question, route, now()),
            ).lastrowid

    def _record_step(
        self,
        run_id: int,
        node_name: str,
        role: str,
        status: str,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        started_at: str,
        latency_ms: int,
    ) -> dict[str, Any]:
        finished_at = now()
        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO agent_steps(run_id, node_name, agent_role, status, input_json, output_json,
                                        latency_ms, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, node_name) DO UPDATE SET
                    agent_role=excluded.agent_role,
                    status=excluded.status,
                    input_json=excluded.input_json,
                    output_json=excluded.output_json,
                    latency_ms=excluded.latency_ms,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at
                """,
                (
                    run_id,
                    node_name,
                    role,
                    status,
                    json.dumps(input_data, ensure_ascii=False),
                    json.dumps(output_data, ensure_ascii=False),
                    latency_ms,
                    started_at,
                    finished_at,
                ),
            )
            step_id = conn.execute(
                "SELECT id FROM agent_steps WHERE run_id=? AND node_name=?", (run_id, node_name)
            ).fetchone()[0]
        return {
            "id": step_id,
            "node": node_name,
            "role": role,
            "status": status,
            "latency_ms": latency_ms,
            "summary": output_data.get("summary", output_data.get("citation_check", "completed")),
        }

    def _timed(self, function: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], int, str]:
        started_at = now()
        started = time.perf_counter()
        output = function()
        return output, round((time.perf_counter() - started) * 1000), started_at

    def process_query(
        self,
        question: str,
        user_name: str = "本机律师",
        use_llm: bool = True,
        persist_memory: bool = True,
        memory_snapshot: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        total_started = time.perf_counter()
        route, plan = self.planner_agent.build_plan(question)
        run_id = self._start_run(question, route)
        steps = []
        active_node, active_role, active_started = "planner", "规划 Agent", now()
        try:
            plan_output = {
                "summary": f"生成 {len(plan)} 节点 DAG",
                "nodes": [
                            {"name": node.name, "role": node.role, "depends_on": list(node.depends_on), "objective": node.objective,
                             "input_scope": node.input_scope, "output_schema": node.output_schema}
                    for node in plan
                ],
            }
            steps.append(self._record_step(run_id, "planner", "规划 Agent", "completed", {"question": question}, plan_output, now(), 0))

            active_node, active_role, active_started = "memory_recall", "记忆 Agent", now()
            memories, memory_ms, memory_started = self._timed(
                lambda: {"items": deepcopy(memory_snapshot) if memory_snapshot is not None else
                         recall_memories(self.case_id, question, 3, self.prefer_remote_embeddings)}
            )
            memories["summary"] = f"召回 {len(memories['items'])} 条案件长期记忆"
            steps.append(self._record_step(run_id, "memory_recall", "记忆 Agent", "completed", {"query": question}, memories, memory_started, memory_ms))

            active_node, active_role, active_started = "retrieve", "检索 Agent", now()
            retrieval, retrieval_ms, retrieval_started = self._timed(lambda: self.retrieval_agent.retrieve(question, 6))
            contexts = retrieval["contexts"]
            retrieval["summary"] = f"双路召回后融合 {len(contexts)} 条页级证据"
            steps.append(self._record_step(run_id, "retrieve", "检索 Agent", "completed", {"query": question}, retrieval, retrieval_started, retrieval_ms))

            specialists: dict[str, SubAgentSpec] = specialist_specs(self)
            specialists = {name: spec for name, spec in specialists.items() if name in {node.name for node in plan}}
            specialist_outputs: dict[str, dict[str, Any]] = {}
            pending = set(specialists)
            dependencies = {node.name: set(node.depends_on) for node in plan}
            completed = {"retrieve"}
            with ThreadPoolExecutor(max_workers=len(specialists), thread_name_prefix="lexvault-agent") as pool:
                # Schedule only nodes whose declared dependencies have completed.
                # Facts/Evidence/Contradiction fan out; Gap Detection joins them.
                while pending:
                    ready = sorted(name for name in pending if dependencies[name] <= completed)
                    if not ready:
                        raise RuntimeError("Invalid or cyclic specialist dependencies")
                    futures = {}
                    for name in ready:
                        spec = specialists[name]
                        function = lambda spec=spec: spec.runner(question, contexts)
                        futures[pool.submit(copy_context().run, self._timed, function)] = (name, spec, now(), time.perf_counter())
                    failures = []
                    for future in as_completed(futures):
                        name, spec, submitted_at, submitted_clock = futures[future]
                        role = spec.role
                        active_node, active_role, active_started = name, role, submitted_at
                        try:
                            output, latency, started_at = future.result()
                            output = validate_specialist_output(output, contexts, schema=spec.output_schema)
                        except Exception as exc:
                            diagnostic = failure_diagnostic(exc, name)
                            steps.append(self._record_step(
                                run_id, name, role, "failed", {"context_count": len(contexts)},
                                {"summary": diagnostic["code"], "diagnostic": diagnostic}, submitted_at,
                                round((time.perf_counter() - submitted_clock) * 1000),
                            ))
                            failures.append((name, role, submitted_at, exc))
                            continue
                        specialist_outputs[name] = output
                        steps.append(self._record_step(
                            run_id, name, role, "completed",
                            {"context_count": len(contexts), "dependencies": sorted(dependencies[name])},
                            output, started_at, latency,
                        ))
                    if failures:
                        active_node, active_role, active_started, error = failures[0]
                        raise error
                    completed.update(ready)
                    pending.difference_update(ready)

            active_node, active_role, active_started = "critic", "审校 Agent", now()
            critic, critic_ms, critic_started = self._timed(
                lambda: self.critic_agent.run(question, route, contexts, specialist_outputs, use_llm)
            )
            critic["summary"] = f"引用检查 {critic['citation_check']}，LLM={'on' if critic['llm_used'] else 'off'}"
            steps.append(self._record_step(run_id, "critic", "审校 Agent", "completed", {"specialists": list(specialist_outputs)}, critic, critic_started, critic_ms))

            active_node, active_role, active_started = "memory", "记忆 Agent", now()
            # 保存疏漏检测结果到数据库
            if "gap_detection" in specialist_outputs:
                gaps = specialist_outputs["gap_detection"].get("gaps", [])
                if gaps:
                    with transaction() as conn:
                        for gap in gaps:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO gap_detections(
                                    case_id, run_id, gap_type, severity, description,
                                    suggestion, affected_evidence_ids, created_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    self.case_id,
                                    run_id,
                                    gap["type"],
                                    gap["severity"],
                                    gap["description"],
                                    gap["suggestion"],
                                    json.dumps(gap.get("affected_evidence_ids", []), ensure_ascii=False),
                                    now()
                                )
                            )

            active_node, active_role, active_started = "memory", "记忆 Agent", now()
            if persist_memory and critic.get("memory_eligible", False):
                stored, store_ms, store_started = self._timed(
                    lambda: {
                        "memory_id": remember(
                            self.case_id, user_name, critic["answer"], run_id, 0.75, self.prefer_remote_embeddings,
                            validated=True,
                        ),
                        "persisted": True,
                    }
                )
                stored["summary"] = "通过结构校验的待律师复核草稿已写入长期记忆"
            else:
                store_started = now()
                store_ms = 0
                stored = {
                    "memory_id": None,
                    "persisted": False,
                    "summary": "对比模式未写入长期记忆" if not persist_memory else "模型失败或答案未通过校验，未写入长期记忆",
                }
            steps.append(self._record_step(run_id, "memory", "记忆 Agent", "completed", {"user_name": user_name}, stored, store_started, store_ms))

            citations = [_citation(item, index) for index, item in enumerate(contexts, 1)]
            total_ms = round((time.perf_counter() - total_started) * 1000)
            with transaction() as conn:
                conn.execute(
                    """UPDATE agent_runs SET status='completed', final_answer=?, citations_json=?,
                       total_ms=?, finished_at=? WHERE id=?""",
                    (critic["answer"], json.dumps(citations, ensure_ascii=False), total_ms, now(), run_id),
                )
            return {
                "run_id": run_id,
                "answer": critic["answer"],
                "route": route,
                "agent_type": "DAG Multi-Agent",
                "plan": plan_output["nodes"],
                "steps": steps,
                "tools_used": ["BM25", "Local Embedding", "RRF", *[spec.role for spec in specialists.values()], "Critic", "Vector Memory"],
                "citations": citations,
                "retrieval_metrics": retrieval["metrics"],
                "memory_hits": memories["items"],
                "llm_used": critic["llm_used"],
                "llm_model": critic["llm_model"],
                "fallback_reason": critic["fallback_reason"],
                "validation": critic.get("validation"),
                "citation_check": critic["citation_check"],
                "failure_diagnostic": critic.get("failure_diagnostic"),
                "memory_persisted": stored["persisted"],
                "total_ms": total_ms,
                "runtime": "native",
                "checkpoint_thread_id": "",
                "resumable": False,
                "resume_count": 0,
            }
        except Exception as exc:
            total_ms = round((time.perf_counter() - total_started) * 1000)
            diagnostic = failure_diagnostic(exc, active_node)
            logger.warning("agent_run_failed runtime=native run_id=%s node=%s error_type=%s",
                           run_id, active_node, type(exc).__name__)
            if not any(step["node"] == active_node for step in steps):
                self._record_step(run_id, active_node, active_role, "failed", {},
                                  {"summary": diagnostic["code"], "diagnostic": diagnostic}, active_started, 0)
            with transaction() as conn:
                conn.execute(
                    "UPDATE agent_runs SET status='failed', final_answer=?, total_ms=?, finished_at=? WHERE id=?",
                    (json.dumps(diagnostic), total_ms, now(), run_id),
                )
            raise


def get_run_trace(run_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return {}
        result = rowdict(run)
        result["resumable"] = False
        if result.get("runtime") == "langgraph" and result["status"] == "failed":
            from .langgraph_agents import LangGraphCoordinator

            result["resumable"] = LangGraphCoordinator(result["case_id"]).checkpoint_available(
                result.get("checkpoint_thread_id", "")
            )
        result["citations"] = json.loads(result.pop("citations_json") or "[]")
        result["steps"] = []
        for row in conn.execute("SELECT * FROM agent_steps WHERE run_id = ? ORDER BY id", (run_id,)):
            item = rowdict(row)
            item["input"] = json.loads(item.pop("input_json") or "{}")
            item["output"] = json.loads(item.pop("output_json") or "{}")
            result["steps"].append(item)
        return result
    finally:
        conn.close()


def create_coordinator(case_id: int, prefer_remote_embeddings: bool = True) -> LawReviewCoordinator:
    return LawReviewCoordinator(case_id, prefer_remote_embeddings)
