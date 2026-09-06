"""Summarize all score-85 attempts without hiding failed or partial runs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_reporting import atomic_json
from scripts.benchmark_campaign import digest, read_rows


def summarize(directory: Path) -> dict[str, Any]:
    campaign = json.loads((directory / "campaign.json").read_text())
    attempts = []
    for output in sorted((directory / "runs").glob("*")):
        if not (output / "summary.json").exists():
            continue
        summary = json.loads((output / "summary.json").read_text())
        rows = read_rows(output / "detailed_results.jsonl")
        verification_file = output / "verification.json"
        verification = json.loads(verification_file.read_text()) if verification_file.exists() else None
        verified = bool(verification and verification["valid"] and verification.get("artifacts") == {
            name: digest(output / name) for name in ("manifest.json", "summary.json", "detailed_results.jsonl")
        })
        attempts.append({
            "run": output.name, "profile": summary["profile"], "split": summary["split"],
            "status": summary["status"], "recorded": summary["total"], "planned": summary["planned"],
            "score_100": summary["score_100"], "failures": summary["failed"],
            "truncated": summary["truncated"], "avg_latency_ms": summary["avg_latency_ms"],
            "request_attempts": summary["model_calls"],
            "responses_received": sum(bool(call.get("finish_reason")) for row in rows for call in row.get("calls", [])),
            "verified": verified,
        })
    result = {"goal_score": 85, "baseline_anchor_score": campaign["baseline_mean_score"] * 100,
              "split_counts": campaign["split_counts"], "attempts": attempts,
              "goal_complete": False, "note": "Development or partial scores do not prove the 1000-question target."}
    atomic_json(directory / "progress.json", result)
    lines = ["# 85 分目标：执行进展", "", "目标尚未完成；下表保留所有尝试，包括服务故障和未完成运行。",
             f"原 1000 题锚点基线：{result['baseline_anchor_score']:.4f} 分。开发集 400 题、独立确认集 200 题已冻结且互不重叠。", "",
             "| 运行 | 集合 | 状态 | 记录/计划 | 均分 | 技术失败 | 收到响应 | 平均秒/题 | 复算 |",
             "|---|---|---|---:|---:|---:|---:|---:|---|"]
    for item in attempts:
        lines.append(f"| {item['run']} | {item['split']} | {item['status']} | {item['recorded']}/{item['planned']} | {item['score_100']:.2f} | {item['failures']} | {item['responses_received']} | {(item['avg_latency_ms'] or 0)/1000:.2f} | {'通过' if item['verified'] else '未完成校验'} |")
    lines += ["", "解释：服务连接失败未产生模型答案，保留零分记录但不用于判断模型能力。任何开发集小样本分数都不能当作 1000 题验收分数。",
              "阶段计划见 docs/benchmarks/SCORE_85_PLAN.md；原始请求、输出、checkpoint 和分任务分数位于每个 runs 子目录。", ""]
    (directory / "PROGRESS.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.directory), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
