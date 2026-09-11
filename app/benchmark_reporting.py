"""Auditable benchmark protocol, all-question summaries and paired reports."""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path

from app.benchmark_metrics import SCORER_VERSION, score_lawbench_item
from app.benchmark_solver import GUIDED_SYSTEM_SUFFIX, TASK_GUIDANCE

PROMPT_VERSION = "lawbench-task-guided-v4"
SYSTEM_PROMPT = "你正在参加中国法律能力评测。严格遵循题目的任务说明和输出格式，直接给出所要求的答案，不展示思维过程，不添加题目未要求的开场白或结论。"

# Tasks whose paired anchor scores improved under task guidance; guidance is
# applied to these only in hybrid mode. Task-level routing, never per-question.
GUIDED_TASK_WHITELIST = frozenset({"2-2", "2-3", "2-4", "2-5", "2-7", "3-2"})


def prompt_for(record: dict, *, strategy: str | None = None) -> tuple[str, str]:
    instruction = record.get("instruction", "").strip()
    if record["dataset"] == "lawbench" and not instruction:
        raise ValueError(f"Missing instruction: {record['question_id']}")
    task = record.get("task")
    guidance = TASK_GUIDANCE.get(task, "") if record["dataset"] == "lawbench" else ""
    strategy = strategy or record.get("prompt_strategy", "task_guided")
    system = SYSTEM_PROMPT
    guided = guidance and (strategy == "task_guided" or (strategy == "hybrid" and task in GUIDED_TASK_WHITELIST))
    if guided:
        system += GUIDED_SYSTEM_SUFFIX + "\n本任务核对方法：" + guidance
    # Keep the original instruction and question verbatim in the user message.
    user = f"{instruction}\n{record['question']}" if instruction else record["question"]
    return system, user


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def source_hashes() -> dict:
    root = Path(__file__).resolve().parent.parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
        "unified_benchmark_runner.py", "app/benchmark_metrics.py", "app/benchmark_reporting.py", "app/lawbench.py",
        "app/benchmark_solver.py", "app/benchmark_postprocess.py",
    )}


def metrics(rows: list[dict]) -> dict:
    latencies = sorted(r["latency_ms"] for r in rows if r.get("latency_ms") is not None)
    return {
        "total": len(rows), "completed": sum(not r.get("error") for r in rows),
        "failed": sum(bool(r.get("error")) for r in rows),
        "empty_responses": sum(not r.get("error") and not r["prediction"].strip() for r in rows),
        "parse_failed": sum(bool(r.get("parse_failed")) for r in rows),
        "reference_invalid": sum(bool(r.get("reference_invalid")) for r in rows),
        "truncated": sum(r.get("finish_reason") == "length" for r in rows),
        "full_score": sum(r["score"] == 1 for r in rows),
        "zero_score": sum(r["score"] == 0 for r in rows),
        "mean_score_all": statistics.mean(r["score"] for r in rows) if rows else 0,
        "avg_latency_ms": statistics.mean(latencies) if latencies else None,
        "p50_latency_ms": statistics.median(latencies) if latencies else None,
        "p95_latency_ms": latencies[min(len(latencies) - 1, int(len(latencies) * .95))] if latencies else None,
    }


def paired_baseline(rows: list[dict], path: Path) -> dict:
    old = {r["question_id"]: r for line in path.read_text(encoding="utf-8").splitlines() if line.strip() for r in [json.loads(line)]}
    paired = []
    for current in rows:
        previous = old.get(current["question_id"])
        if previous is None or previous["question"] != current["question"] or previous["reference"] != current["reference"]:
            raise ValueError(f"Baseline missing/mismatched question: {current['question_id']}")
        rescored = score_lawbench_item(previous["task"], previous["prediction"], previous["reference"], question=previous["question"]).to_dict() if not previous.get("error") else {"score": 0, "parse_failed": False}
        paired.append({"question_id": current["question_id"], "task": current["task"], "old_stored_score": previous["score"], "old_rescored_score": rescored["score"], "new_score": current["score"], "delta": current["score"] - rescored["score"]})
    by_task = defaultdict(list)
    for r in paired:
        by_task[r["task"]].append(r)
    def aggregate(rr):
        return {"n": len(rr), **{k: statistics.mean(r[k] for r in rr) for k in ("old_stored_score", "old_rescored_score", "new_score", "delta")}, "improved": sum(r["delta"] > 1e-9 for r in rr), "regressed": sum(r["delta"] < -1e-9 for r in rr)}
    return {"baseline": str(path.resolve()), "overall": aggregate(paired), "tasks": {k: aggregate(v) for k, v in by_task.items()}, "items": paired}


def write_report(rows: list[dict], output_dir: Path, duration: float, model_config: dict, manifest: dict, status: str) -> dict:
    tasks = defaultdict(list)
    datasets = defaultdict(list)
    for r in rows:
        tasks[r["task"]].append(r)
        datasets[r["dataset"]].append(r)
    summary = {
        "run_id": output_dir.name, "status": status, "planned_questions": manifest["planned_questions"],
        "total_questions": len(rows), "duration_hours": duration / 3600, "model_config": model_config,
        "scorer_version": SCORER_VERSION, "prompt_version": PROMPT_VERSION,
        "metric_note": "Project-local mixed metric mean, not accuracy or official LawBench leaderboard score. All attempted questions, including errors/empty/parse failures, remain in denominator.",
        **metrics(rows), "tasks": {k: {"name": v[0].get("task_name", k), "metric": v[0]["metric"], **metrics(v)} for k, v in tasks.items()},
        "datasets": {k: metrics(v) for k, v in datasets.items()},
    }
    if manifest.get("baseline_results") and rows:
        comparison = paired_baseline(rows, Path(manifest["baseline_results"]))
        atomic_json(output_dir / "paired_comparison.json", comparison)
        summary["paired_baseline"] = comparison["overall"]
    atomic_json(output_dir / "summary.json", summary)
    lines = ["# LawBench 1,000 题修复验证报告" if manifest["planned_questions"] == 1000 else "# LawBench 修复验证报告",
        "", f"状态：{status}；已记录 {len(rows)}/{manifest['planned_questions']} 题。", "",
        f"模型：`{model_config['model']}`；temperature={model_config['temperature']}，max_tokens={model_config['max_tokens']}，thinking={model_config['enable_thinking']}。",
        f"评分：`{SCORER_VERSION}`；提示词：`{PROMPT_VERSION}`；策略：`{manifest.get('prompt_strategy', 'task_guided')}`；抽样种子：{manifest.get('sample_seed')}。", "",
        "## 总览", "", f"- 全题混合均分：{summary['mean_score_all']:.2%}（不剔除失败题）。",
        f"- 请求成功：{summary['completed']}/{len(rows)}；技术失败：{summary['failed']}；空响应：{summary['empty_responses']}；解析失败：{summary['parse_failed']}；截断：{summary['truncated']}。",
        f"- 非月数参考答案等不可评分数据：{summary['reference_invalid']} 题，单独标记，仍以零分保留在全题均分中。",
        f"- 累计执行耗时：{duration / 3600:.2f} 小时；平均请求耗时（含重试/退避）：{(summary['avg_latency_ms'] or 0) / 1000:.2f} 秒。", ""]
    if "paired_baseline" in summary:
        p = summary["paired_baseline"]
        lines += ["## 同题配对比较", "", f"旧回答原评分：{p['old_stored_score']:.2%}；旧回答按新规则重评：{p['old_rescored_score']:.2%}；新回答：{p['new_score']:.2%}。",
                  f"同评分规则下变化：{p['delta'] * 100:+.2f} 个百分点；提升 {p['improved']} 题，下降 {p['regressed']} 题。", "",
                  "旧回答来自已保存的全量运行，不额外调用旧提示词；原评分仅作历史参考。"]
    lines += ["", "## 分任务", "", "| 任务 | 题数 | 混合均分/任务分数 | 解析失败 | 技术失败 |", "|---|---:|---:|---:|---:|"]
    for task, m in summary["tasks"].items():
        lines.append(f"| {task} {m['name']} | {m['total']} | {m['mean_score_all']:.2%} | {m['parse_failed']} | {m['failed']} |")
    lines += ["", "## 方法和限制", "",
        "- 20 类分层抽样，抽样清单在调用模型前冻结；每题完整传入原始 instruction 和 question，不向模型提供 reference。",
        "- 不按分数筛题、不重试低分题、不使用标准答案修复模型输出；仅网络/服务错误允许按配置重试。",
        "- 更换的是提示词和评测协议，没有训练或更换模型。旧结果的评分缺陷会造成原分数和新分数不可直接比较。",
        "- 这是项目本地评分器，不是官方排行榜评分：校对使用字符编辑 F0.5，阅读理解及实体/触发词分数为本地近似。",
        "- 上游刑期评分会跳过无期/死刑参考答案；本报告不静默剔除，单列 reference_invalid，不能据此评价模型的刑期预测能力。",
        "- 解析器采取保守的答案格式识别；长篇解释、歧义答案可能被记为解析失败或零分，不代表人工法律结论。",
        "- 每题只生成一次（技术重试除外），温度 0 仍不保证服务完全确定；1,000 题不能代表所有法律场景。",
        "- 此测试直接调用模型，不经过 RAG、原生 DAG 或 LangGraph，不能用来证明框架之间的质量差异。",
        "- 原始回答、finish_reason、usage、所有尝试错误、完整提示词和源码哈希均保留在本运行目录。",
        "", "数据来源：[LawBench 固定版本](https://github.com/open-compass/LawBench/tree/e30981bb3ff54c41571f222e0b23e92d27375388)。", ""]
    (output_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return summary
