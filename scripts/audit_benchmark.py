"""Re-score immutable LawBench answers into a new directory without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_metrics import SCORER_VERSION, score_lawbench_item
from app.benchmark_reporting import atomic_json, source_hashes, write_report
from verify_benchmark_run import verify


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def paired_interval(items: list[dict[str, Any]], repetitions: int = 10000) -> dict[str, Any]:
    """Stratified paired percentile bootstrap: fixed task sizes, seed 42."""
    rng = random.Random(42)
    groups: dict[str, list[float]] = defaultdict(list)
    for item in items:
        groups[item["task"]].append(item["delta"])
    samples = sorted(
        sum(sum(rng.choices(values, k=len(values))) for values in groups.values()) / len(items)
        for _ in range(repetitions)
    )
    return {
        "method": "paired percentile bootstrap stratified by task",
        "seed": 42, "repetitions": repetitions, "confidence": 0.95,
        "lower": samples[int(repetitions * 0.025)], "upper": samples[int(repetitions * 0.975) - 1],
        "note": "Conditional on saved answers; excludes model-run, dataset-contamination and scorer uncertainty.",
    }


def audit(source: Path, destination: Path) -> dict[str, Any]:
    source = source.resolve()
    destination = destination.resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Audit output must be a separate directory, not nested in the source run")
    if destination.exists():
        raise FileExistsError("Audit output already exists; choose a new directory")
    integrity = verify(source, artifacts_only=True, write=False)
    if not integrity["valid"]:
        raise ValueError(f"Source artifacts failed validation: {integrity['issues']}")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    original_summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    originals = [json.loads(line) for line in (source / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(row["dataset"] != "lawbench" for row in originals):
        raise ValueError("This audit currently supports LawBench only")
    rows = []
    changes = []
    for original in originals:
        scoring = score_lawbench_item(original["task"], original["prediction"], original["reference"], question=original["question"]).to_dict() if not original.get("error") else {
            "score": 0.0, "metric": "error", "abstained": False, "parse_failed": False,
            "parsed_prediction": None, "parsed_reference": None, "reference_invalid": False,
        }
        row = {**original, **scoring, "scorer_version": SCORER_VERSION,
               "original_scoring": {key: original.get(key) for key in (*scoring, "scorer_version")}}
        rows.append(row)
        changed_fields = [key for key in scoring if original.get(key) != scoring[key]]
        if changed_fields:
            changes.append({"question_id": row["question_id"], "task": row["task"], "changed_fields": changed_fields,
                            "original_score": original["score"], "audited_score": row["score"], "delta": row["score"] - original["score"]})
    protected = ["manifest.json", "detailed_results.jsonl", "summary.json", "REPORT.md"]
    protected += [name for name in ("verification.json", "paired_comparison.json") if (source / name).exists()]
    input_hashes = {name: sha256(source / name) for name in protected}
    destination.mkdir(parents=True)
    new_manifest = {
        **manifest, "scorer_version": SCORER_VERSION, "source_hashes": source_hashes(),
        "audit": {"source_run": str(source), "rescore_only": True, "new_model_calls": 0,
                  "original_scorer_version": manifest["scorer_version"], "input_hashes": input_hashes,
                  "script_sha256": sha256(Path(__file__)), "source_integrity": integrity},
    }
    atomic_json(destination / "manifest.json", new_manifest)
    for name in new_manifest["source_hashes"]:
        path = destination / "source_snapshot" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    audit_script = destination / "source_snapshot" / "scripts" / "audit_benchmark.py"
    audit_script.parent.mkdir(parents=True, exist_ok=True)
    audit_script.write_bytes(Path(__file__).read_bytes())
    (destination / "checkpoints").mkdir()
    for row in rows:
        atomic_json(destination / "checkpoints" / f"{row['question_id']}.json", row)
    (destination / "detailed_results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    summary = write_report(rows, destination, original_summary["duration_hours"] * 3600, manifest["model_config"], new_manifest, original_summary["status"])
    comparison = json.loads((destination / "paired_comparison.json").read_text(encoding="utf-8")) if manifest.get("baseline_results") else None
    interval = paired_interval(comparison["items"]) if comparison else None
    changed_scores = [row for row in changes if "score" in row["changed_fields"]]
    result = {
        "source_run": str(source), "rescore_only": True, "new_model_calls": 0,
        "original_scorer": manifest["scorer_version"], "audited_scorer": SCORER_VERSION,
        "original_mean": original_summary["mean_score_all"], "audited_mean": summary["mean_score_all"],
        "score_delta": summary["mean_score_all"] - original_summary["mean_score_all"],
        "changed_score_count": len(changed_scores), "changed_field_count": len(changes),
        "original_parse_failed": original_summary["parse_failed"], "audited_parse_failed": summary["parse_failed"],
        "paired_delta_interval": interval, "changes": changes,
        "input_hashes_unchanged": all(sha256(source / name) == digest for name, digest in input_hashes.items()),
    }
    verification = verify(destination)
    result["verification"] = verification
    atomic_json(destination / "audit.json", result)
    report = destination / "REPORT.md"
    banner = [
        f"> 离线评分审计：复用 {source.name} 的原始回答，新增模型调用 0 次。原运行文件未改动。",
        f"> 原 {manifest['scorer_version']} 均分 {original_summary['mean_score_all']:.2%}；审计后 {SCORER_VERSION} 均分 {summary['mean_score_all']:.2%}；仅评分修正影响 {result['score_delta'] * 100:+.2f} 个百分点、{len(changed_scores)} 题。",
        f"> 原“解析失败” {original_summary['parse_failed']} 题，修正后 {summary['parse_failed']} 题；未知/错误类别仍计入预测和零分，不等于格式提取失败。",
        "", "",
    ]
    detail = ["", "## 本轮评分修正与可复核性", "",
        "- 罪名标签按分号分隔；复合罪名内部的顿号和逗号保留。统一全/半角、空白、罪名后缀“罪”等展示差异。",
        "- 未命中全局类别表不再误标为解析失败；不做语义近似匹配、不从分析正文挖掘正确标签、不按当前参考答案定制提取。",
        "- parsed_prediction 直接记录参与评分的集合，多余或未知标签会降低分数。",
        "- 上游罪名参考答案按分号拆分，但预测使用全文选项扫描；本项目仍用更严格的答案段解析，不能称官方评分器。",
        "- 原输入、旧基线、源码快照和每题 checkpoint 完整性已检查；新答案重评及同题旧答案重评均通过当前评分复算。",
        "- audit.json 列出全部逐题变化，manifest.json 记录原文件 SHA-256 与审计脚本 SHA-256。",
        "", "上游规则：[固定 commit 的罪名评分代码](https://github.com/open-compass/LawBench/blob/e30981bb3ff54c41571f222e0b23e92d27375388/evaluation/evaluation_functions/ljp_accusation.py)。"]
    if interval:
        detail += ["", f"按任务分层、同题配对的 10,000 次 bootstrap：均分差 95% 区间 [{interval['lower'] * 100:+.2f}, {interval['upper'] * 100:+.2f}] 个百分点（seed=42）。",
                   "该区间仅描述这些已保存回答的抽样变化，未覆盖模型重复运行、数据污染或评分器本身的不确定性。"]
    report.write_text("\n".join(banner) + report.read_text(encoding="utf-8") + "\n".join(detail) + "\n", encoding="utf-8")
    if not result["input_hashes_unchanged"] or not verification["valid"]:
        raise ValueError("Audit verification failed; inspect audit.json")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = audit(args.source, args.destination)
    print(json.dumps({key: value for key, value in result.items() if key not in {"changes", "verification"}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
