#!/usr/bin/env python3
"""Run a reproducible native-DAG versus LangGraph comparison."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from app.agents import create_coordinator, get_run_trace
from app.db import init_db
from app.langgraph_agents import create_langgraph_coordinator
from app.runtime_comparison import compare_runtime_results
from app.rag import recall_memories
from app.services import LOCAL_LLM_MODEL, local_llm_available


CASES = [
    {"id": "facts", "question": "梳理募集资金的主要流向。"},
    {"id": "statistics", "question": "募集资金总额和投资人数是多少？"},
    {"id": "contradiction", "question": "张某关于固定回报审批的陈述是否矛盾？"},
    {"id": "gap_detection", "question": "检查本案证据疏漏和待补证事项。"},
]


def runtime_stats(records: list[dict[str, Any]], runtime: str, expected: int) -> dict[str, Any]:
    selected = [
        record
        for record in records
        if record["runtime"] == runtime and record.get("ok") and record.get("llm_used")
    ]
    latencies = [record["total_ms"] for record in selected]
    return {
        "valid_runs": len(selected),
        "expected_runs": expected,
        "success_rate": round(len(selected) / expected, 4) if expected else 0.0,
        "mean_ms": round(statistics.mean(latencies)) if latencies else None,
        "median_ms": round(statistics.median(latencies)) if latencies else None,
        "min_ms": min(latencies) if latencies else None,
        "max_ms": max(latencies) if latencies else None,
    }


def render_report(payload: dict[str, Any]) -> str:
    stats = payload["statistics"]
    native = stats["native"]
    graph = stats["langgraph"]
    pairs = payload["pairs"]
    valid_pairs = [item for item in pairs if item.get("comparison")]
    structurally_equivalent = sum(
        bool(item["comparison"]["structurally_equivalent"]) for item in valid_pairs
    )
    fully_accepted = sum(bool(item["comparison"]["equivalent"]) for item in valid_pairs)
    checkpoint_growth = [
        record.get("checkpoint_growth_bytes", 0)
        for record in payload["runs"]
        if record["runtime"] == "langgraph" and record.get("ok")
    ]
    average_growth = round(statistics.mean(checkpoint_growth)) if checkpoint_growth else 0
    paired_native = [item["comparison"]["native_total_ms"] for item in valid_pairs]
    paired_graph = [item["comparison"]["langgraph_total_ms"] for item in valid_pairs]
    paired_native_mean = round(statistics.mean(paired_native)) if paired_native else None
    paired_graph_mean = round(statistics.mean(paired_graph)) if paired_graph else None
    paired_native_median = round(statistics.median(paired_native)) if paired_native else None
    paired_graph_median = round(statistics.median(paired_graph)) if paired_graph else None
    mean_delta = (
        paired_graph_mean - paired_native_mean
        if paired_graph_mean is not None and paired_native_mean is not None
        else None
    )
    overhead = (
        round(mean_delta / paired_native_mean * 100, 2)
        if mean_delta is not None and paired_native_mean
        else None
    )

    rows = []
    for item in pairs:
        comparison = item.get("comparison")
        if comparison:
            rows.append(
                f"| {item['round']} | {item['case_id']} | "
                f"{'是' if comparison['structurally_equivalent'] else '否'} | "
                f"{'通过' if comparison['answer_contract_match'] else '未通过'} | "
                f"{comparison['native_total_ms']} | {comparison['langgraph_total_ms']} | "
                f"{comparison['latency_delta_ms']:+d} |"
            )
        else:
            rows.append(
                f"| {item['round']} | {item['case_id']} | 无效 | 无效 | — | — | — |"
            )
    delta_text = "无有效样本" if mean_delta is None else f"{mean_delta:+d} ms（{overhead:+.2f}%）"
    structural_text = f"{structurally_equivalent}/{len(valid_pairs)}" if valid_pairs else "0/0"
    acceptance_text = f"{fully_accepted}/{len(valid_pairs)}" if valid_pairs else "0/0"
    engineering = payload.get("engineering", {})
    source_lines = engineering.get("source_lines", {})
    complexity_text = "、".join(f"`{name}` {count} 行" for name, count in source_lines.items())
    verification_text = "\n".join(f"- {item}" for item in payload.get("verification", []))
    quality_text = "\n".join(f"- {item}" for item in payload.get("quality_notes", []))
    round_rows = []
    for round_number in sorted({record["round"] for record in payload["runs"]}):
        records = [record for record in payload["runs"] if record["round"] == round_number and record.get("llm_used")]
        left = [record["total_ms"] for record in records if record["runtime"] == "native"]
        right = [record["total_ms"] for record in records if record["runtime"] == "langgraph"]
        if len(left) == len(CASES) and len(right) == len(CASES):
            left_mean, right_mean = statistics.mean(left), statistics.mean(right)
            round_rows.append(f"| {round_number} | {left_mean:.0f} ms | {right_mean:.0f} ms | {right_mean - left_mean:+.0f} ms |")
    return f"""# 原生 DAG 与 LangGraph 实测对比报告

生成时间：{payload['generated_at']}
模型：`{payload['model']}`
Python：`{payload['python']}`
LangGraph：`{payload['langgraph_version']}`
基准最大输出：`{payload['max_tokens']}` tokens

## 结论摘要

- 有效运行：原生 {native['valid_runs']}/{native['expected_runs']}，LangGraph {graph['valid_runs']}/{graph['expected_runs']}。
- 核心结构等价：{structural_text} 组有效配对的路由、引用集合、节点集合和专家输出一致。
- 完整答案契约：{acceptance_text} 组同时满足非空答案、有效引用编号和律师复核提示。模型能完成调用，不代表答案已经通过业务验收。
- 配对平均耗时：原生 {paired_native_mean} ms，LangGraph {paired_graph_mean} ms，差值 {delta_text}。
- LangGraph checkpoint 每次运行平均增长约 {average_growth:,} 字节。

| 运行时 | 有效率 | 配对平均耗时 | 配对中位数 | 配对最小值 | 配对最大值 |
|---|---:|---:|---:|---:|---:|
| 原生 DAG | {native['success_rate']:.0%} | {paired_native_mean} ms | {paired_native_median} ms | {min(paired_native) if paired_native else None} ms | {max(paired_native) if paired_native else None} ms |
| LangGraph | {graph['success_rate']:.0%} | {paired_graph_mean} ms | {paired_graph_median} ms | {min(paired_graph) if paired_graph else None} ms | {max(paired_graph) if paired_graph else None} ms |

## 配对结果

| 轮次 | 场景 | 核心结构 | 答案契约 | 原生耗时 ms | LangGraph 耗时 ms | 差值 ms |
|---:|---|---|---|---:|---:|---:|
{chr(10).join(rows)}

### 按轮次观察顺序与热机效应

| 轮次 | 原生平均 | LangGraph 平均 | 差值 |
|---:|---:|---:|---:|
{chr(10).join(round_rows)}

若分轮结果方向不同，应优先解释为模型状态、执行顺序与采样噪声，不将整体均值当作框架稳定加速的证据。

## 加入 LangGraph 的优点

1. **真实依赖图**：Facts、Evidence 并行执行，Gap Detection 明确等待其依赖完成；图结构不再只是展示元数据。
2. **失败续跑**：节点级 checkpoint 可从失败位置恢复。受控恢复测试验证 Critic/LLM 不会因后续 Memory 失败而重复调用。
3. **业务等价可验证**：两版复用同一批业务 Agent，并通过结构化结果而非随机正文逐字比较，当前核心结构等价率为 {structural_text}。
4. **扩展边界清晰**：未来加入人工复核、条件重试或分支时，可通过节点和边扩展，无需继续扩大手写协调器。

## 缺点与成本

1. **依赖明显增加**：除 `langgraph` 外还会安装 checkpoint、LangChain Core、序列化和 SDK 等传递依赖。
2. **本地存储增长**：本轮 SQLite checkpoint 平均每次增加约 {average_growth:,} 字节，并会保存证据状态副本，需要纳入敏感数据和保留策略。
3. **没有稳定提速证据**：有效配对的平均差异为 {delta_text}，且不同问题正负波动明显；LangGraph 的价值应按恢复能力和维护性评估，而不是推理速度。
4. **双重可观测性**：产品仍需维护 `agent_runs/agent_steps` 审计表，同时 LangGraph 维护内部 checkpoint；两者职责必须保持分离。
5. **SQLite 扩展上限**：当前 saver 适合单机原型，不适合作为多实例、高并发生产部署的最终方案。

## 代码和验证成本

- 新增两个直接依赖：`langgraph>=1.2,<2`、`langgraph-checkpoint-sqlite>=3.1,<4`；未启用云服务或 LangSmith 跟踪。
- 当前源代码行数（包含注释和空行，不是圈复杂度）：{complexity_text}。
- 原生协调器继续保留。维护成本来自第二套调度器、状态序列化、恢复幂等和双重审计，而非业务 Agent 的复制。
{verification_text}

## 模型答案质量观察

{quality_text or '答案契约只检查非空、引用编号和复核提示，不代替语义、算术或法律正确性审查。'}

## 判定方法与限制

- 共 4 个场景、{payload['rounds']} 轮、2 个运行时，共记录 {payload['completed_model_calls']} 次执行；偶数轮反转运行顺序以减轻热机偏差。
- 只有 `llm_used=true` 的运行计为有效；模型超时或规则降级不会混入延迟均值。
- 性能表只使用两版都有效的配对样本；原始 JSON 同时保留各运行记录、完整答案、引用和产品节点输入输出。
- 每组对比冻结同一份执行前记忆，且两版均关闭长期记忆写入。
- “核心结构等价”要求路由、引用集合、已完成节点和专家结构化输出一致；“完整验收”还要求答案非空、引用编号有效并包含律师复核提示。
- Gap Detection 本次修正的是执行依赖顺序，业务算法仍复用原实现；没有证据表明仅加入框架就能提高法律分析质量。
- 本次最大输出设置为 {payload['max_tokens']} tokens，生产默认仍为 900。短输出可能截断引用或复核提示；未采集模型 `finish_reason`，不把答案契约失败全部归因为截断，也不据此宣称两版答案质量等价。
- 仅两轮样本且共享同一本地模型服务，延迟容易受模型缓存和机器负载影响；性能结果用于工程取舍，不代表统计显著性。
"""


def run_benchmark(
    case_id: int,
    rounds: int,
    progress_json: str | None = None,
    progress_report: str | None = None,
    max_tokens: int = 320,
) -> dict[str, Any]:
    os.environ["LAW_REVIEW_LLM_MAX_TOKENS"] = str(max_tokens)
    available, model = local_llm_available(LOCAL_LLM_MODEL)
    if not available:
        raise RuntimeError(f"本地模型不可用：{model}")
    init_db(seed=True)
    runs: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    expected_per_runtime = len(CASES) * rounds

    def snapshot() -> dict[str, Any]:
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": model,
            "python": platform.python_version(),
            "langgraph_version": version("langgraph"),
            "case_id": case_id,
            "rounds": rounds,
            "cases": CASES,
            "expected_model_calls": expected_per_runtime * 2,
            "max_tokens": max_tokens,
            "completed_model_calls": len(runs),
            "runs": runs,
            "pairs": pairs,
            "statistics": {
                "native": runtime_stats(runs, "native", expected_per_runtime),
                "langgraph": runtime_stats(runs, "langgraph", expected_per_runtime),
            },
            "engineering": engineering_metrics(),
        }

    def persist_progress() -> None:
        payload = snapshot()
        if progress_json:
            Path(progress_json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if progress_report:
            Path(progress_report).write_text(render_report(payload), encoding="utf-8")

    for round_number in range(1, rounds + 1):
        order = ("native", "langgraph") if round_number % 2 else ("langgraph", "native")
        for case in CASES:
            results: dict[str, dict[str, Any]] = {}
            memory_snapshot = recall_memories(case_id, case["question"], 3, True)
            for runtime in order:
                print(
                    f"[{len(runs) + 1}/{expected_per_runtime * 2}] round={round_number} "
                    f"case={case['id']} runtime={runtime} starting",
                    flush=True,
                )
                started = time.perf_counter()
                try:
                    if runtime == "native":
                        result = create_coordinator(case_id, True).process_query(
                            case["question"], "框架对比", True,
                            persist_memory=False, memory_snapshot=memory_snapshot,
                        )
                        checkpoint_growth = 0
                    else:
                        coordinator = create_langgraph_coordinator(case_id, True)
                        checkpoint_before = coordinator.checkpoint_size_bytes()
                        result = coordinator.process_query(
                            case["question"], "框架对比", True,
                            persist_memory=False, memory_snapshot=memory_snapshot,
                        )
                        checkpoint_growth = max(
                            0, coordinator.checkpoint_size_bytes() - checkpoint_before
                        )
                    result["checkpoint_growth_bytes"] = checkpoint_growth
                    results[runtime] = result
                    runs.append(
                        {
                            "round": round_number,
                            "case_id": case["id"],
                            "question": case["question"],
                            "runtime": runtime,
                            "ok": True,
                            "run_id": result["run_id"],
                            "total_ms": result["total_ms"],
                            "wall_ms": round((time.perf_counter() - started) * 1000),
                            "llm_used": result["llm_used"],
                            "fallback_reason": result["fallback_reason"],
                            "checkpoint_growth_bytes": checkpoint_growth,
                            "result": result,
                            "trace": get_run_trace(result["run_id"]),
                        }
                    )
                    print(
                        f"  completed run_id={result['run_id']} total_ms={result['total_ms']} "
                        f"llm_used={result['llm_used']}",
                        flush=True,
                    )
                except Exception as exc:
                    runs.append(
                        {
                            "round": round_number,
                            "case_id": case["id"],
                            "question": case["question"],
                            "runtime": runtime,
                            "ok": False,
                            "wall_ms": round((time.perf_counter() - started) * 1000),
                            "error": str(exc),
                        }
                    )
                    print(f"  failed: {exc}", flush=True)
                persist_progress()
            valid = all(
                runtime in results and results[runtime].get("llm_used")
                for runtime in ("native", "langgraph")
            )
            pairs.append(
                {
                    "round": round_number,
                    "case_id": case["id"],
                    "question": case["question"],
                    "execution_order": list(order),
                    "native_run_id": results.get("native", {}).get("run_id"),
                    "langgraph_run_id": results.get("langgraph", {}).get("run_id"),
                    "comparison": compare_runtime_results(
                        results["native"], results["langgraph"]
                    )
                    if valid
                    else None,
                }
            )
            persist_progress()

    return snapshot()


def engineering_metrics() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    names = ["app/agents.py", "app/langgraph_agents.py", "app/runtime_comparison.py",
             "compare_agent_runtimes.py", "tests/test_04_langgraph_runtime.py"]
    return {"source_lines": {name: len((root / name).read_text().splitlines()) for name in names}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--json", default="langgraph_comparison_results.json")
    parser.add_argument("--report", default="LANGGRAPH_COMPARISON_REPORT.md")
    parser.add_argument("--max-tokens", type=int, default=320)
    parser.add_argument("--refresh-report", action="store_true", help="Regenerate a report from existing JSON without model calls")
    args = parser.parse_args()
    if args.refresh_report:
        payload = json.loads(Path(args.json).read_text(encoding="utf-8"))
        payload["engineering"] = engineering_metrics()
        records = {record.get("run_id"): record for record in payload["runs"]}
        for pair in payload["pairs"]:
            native = records.get(pair.get("native_run_id"), {})
            graph = records.get(pair.get("langgraph_run_id"), {})
            if native.get("llm_used") and graph.get("llm_used") and native.get("result") and graph.get("result"):
                pair["comparison"] = compare_runtime_results(
                    native["result"], graph["result"], native.get("trace"), graph.get("trace")
                )
    else:
        if args.rounds < 1 or args.max_tokens < 1:
            parser.error("rounds and max-tokens must be positive")
        payload = run_benchmark(
            args.case_id, args.rounds, args.json, args.report, args.max_tokens
        )
    Path(args.json).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path(args.report).write_text(render_report(payload), encoding="utf-8")
    print(json.dumps({"statistics": payload["statistics"], "pairs": payload["pairs"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
