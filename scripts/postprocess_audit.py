"""Replay reference-free output post-processing on saved answers; never calls a model.

This complements ``audit_benchmark.py``: that tool re-scores stored answers with the
current scorer, while this tool first applies the project's deterministic,
gold-blind output repairs (``app/benchmark_postprocess.py``) and then re-scores.
The source run is never modified; every row keeps its original prediction and
score, and ``audit.json`` records per-task and per-policy deltas.

Only branches that need no retrieval context are replayed. Task 1-1 depends on an
official statutory hit recorded at inference time, so it is deliberately out of
scope here and must be measured by a real run with the statutory corpus.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.benchmark_metrics import SCORER_VERSION, parse_label_answer, score_lawbench_item, task_label_space
from app.benchmark_postprocess import POSTPROCESS_VERSION, postprocess
from app.benchmark_reporting import atomic_json, source_hashes, write_report
from scripts.audit_benchmark import paired_interval, sha256
from verify_benchmark_run import verify

# Post-processing branches that are pure functions of the question and the saved
# prediction. 1-1 is excluded: it needs the exact-article retrieval recorded at
# inference time and cannot be replayed offline.
REPLAY_TASKS = frozenset({"2-1", "2-7", "2-9", "2-10"})
CHARGE_TASK = "3-3"
CHARGE_POLICY = "charge-ontology-unique-superstring; no reference access"
UNCHANGED_POLICY = "unchanged"


def canonicalize_charges(prediction: str) -> tuple[str, list[str]]:
    """Map an out-of-ontology charge to the only ontology label that contains it.

    The global charge ontology is the task's declared output space, never the
    current item's reference answer. A charge is rewritten only when exactly one
    ontology label contains it (for example ``虚开增值税专用发票`` ->
    ``虚开增值税专用发票、用于骗取出口退税、抵扣税款发票``); ambiguous or unknown
    charges are left untouched, so a rewrite can only ever replace a label that
    could never match a reference with a candidate that can.
    """
    ontology = task_label_space(CHARGE_TASK)
    labels = parse_label_answer(CHARGE_TASK, prediction, ontology)
    if not labels:
        return prediction, []
    revised: set[str] = set()
    mapped: list[str] = []
    for label in sorted(labels):
        if label in ontology:
            revised.add(label)
            continue
        candidates = sorted(candidate for candidate in ontology if label and (label in candidate or candidate in label))
        if len(candidates) == 1:
            revised.add(candidates[0])
            mapped.append(f"{label} -> {candidates[0]}")
        else:
            revised.add(label)
    if not mapped:
        return prediction, []
    return "罪名：" + ";".join(sorted(revised)), mapped


def _aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"n": 0, "before": None, "after": None, "delta": None, "improved": 0, "regressed": 0}
    before = statistics.mean(item["before"] for item in items)
    after = statistics.mean(item["after"] for item in items)
    return {
        "n": len(items), "before": before, "after": after, "delta": after - before,
        "improved": sum(item["after"] > item["before"] + 1e-9 for item in items),
        "regressed": sum(item["after"] < item["before"] - 1e-9 for item in items),
    }


def replay(source: Path, destination: Path, *, charge_canonicalization: bool = False) -> dict[str, Any]:
    """Apply gold-blind post-processing to stored answers and re-score them."""
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
    originals = [
        json.loads(line)
        for line in (source / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if any(row["dataset"] != "lawbench" for row in originals):
        raise ValueError("This audit currently supports LawBench only")

    rows: list[dict[str, Any]] = []
    paired: list[dict[str, Any]] = []
    policy_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    changes: list[dict[str, Any]] = []
    for original in originals:
        task = original["task"]
        revised = original["prediction"]
        audit: dict[str, Any] = {"version": POSTPROCESS_VERSION, "applied": False, "policy": UNCHANGED_POLICY}
        if not original.get("error"):
            if task in REPLAY_TASKS:
                revised, audit = postprocess(task, original["question"], revised, None)
            if task == CHARGE_TASK and charge_canonicalization:
                revised, mapped = canonicalize_charges(revised)
                if mapped:
                    audit = {
                        "version": POSTPROCESS_VERSION, "applied": True,
                        "policy": CHARGE_POLICY, "mapped_charges": mapped,
                    }
        if original.get("error"):
            scoring = {
                "score": 0.0, "metric": "error", "abstained": False, "parse_failed": False,
                "parsed_prediction": None, "parsed_reference": None, "reference_invalid": False,
            }
        else:
            scoring = score_lawbench_item(
                task, revised, original["reference"], question=original["question"],
            ).to_dict()
        row = {
            **original, **scoring, "prediction": revised, "scorer_version": SCORER_VERSION,
            "original_prediction": original["prediction"], "original_score": original["score"],
            "postprocess": audit,
        }
        rows.append(row)
        before, after = float(original["score"]), float(row["score"])
        paired.append({
            "question_id": row["question_id"], "task": task,
            "before": before, "after": after, "delta": after - before,
        })
        policy_items[str(audit["policy"])].append({"before": before, "after": after})
        if revised != original["prediction"] or after != before:
            changes.append({
                "question_id": row["question_id"], "task": task, "policy": audit["policy"],
                "original_score": before, "audited_score": after, "delta": after - before,
                "original_prediction": original["prediction"], "audited_prediction": revised,
            })

    protected = ["manifest.json", "detailed_results.jsonl", "summary.json", "REPORT.md"]
    protected += [name for name in ("verification.json", "paired_comparison.json") if (source / name).exists()]
    input_hashes = {name: sha256(source / name) for name in protected}
    destination.mkdir(parents=True)
    new_manifest = {
        **manifest, "scorer_version": SCORER_VERSION, "source_hashes": source_hashes(),
        "audit": {
            "source_run": str(source), "rescore_only": True, "new_model_calls": 0,
            "postprocess_version": POSTPROCESS_VERSION,
            "replayed_tasks": sorted(REPLAY_TASKS),
            "charge_canonicalization": charge_canonicalization,
            "original_scorer_version": manifest["scorer_version"], "input_hashes": input_hashes,
            "script_sha256": sha256(Path(__file__)), "source_integrity": integrity,
        },
    }
    atomic_json(destination / "manifest.json", new_manifest)
    for name in new_manifest["source_hashes"]:
        path = destination / "source_snapshot" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    audit_script = destination / "source_snapshot" / "scripts" / "postprocess_audit.py"
    audit_script.parent.mkdir(parents=True, exist_ok=True)
    audit_script.write_bytes(Path(__file__).read_bytes())
    (destination / "checkpoints").mkdir()
    for row in rows:
        atomic_json(destination / "checkpoints" / f"{row['question_id']}.json", row)
    (destination / "detailed_results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8",
    )
    summary = write_report(
        rows, destination, original_summary["duration_hours"] * 3600, manifest["model_config"],
        new_manifest, original_summary["status"],
    )
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in paired:
        by_task[item["task"]].append(item)
    interval = paired_interval(paired)
    result = {
        "source_run": str(source), "rescore_only": True, "new_model_calls": 0,
        "postprocess_version": POSTPROCESS_VERSION, "charge_canonicalization": charge_canonicalization,
        "original_scorer": manifest["scorer_version"], "audited_scorer": SCORER_VERSION,
        "original_mean": original_summary["mean_score_all"], "audited_mean": summary["mean_score_all"],
        "score_delta": summary["mean_score_all"] - original_summary["mean_score_all"],
        "changed_count": len(changes),
        "tasks": {
            task: _aggregate(items) | {
                "name": next(row["task_name"] for row in rows if row["task"] == task),
                "metric": next(row["metric"] for row in rows if row["task"] == task),
            }
            for task, items in sorted(by_task.items())
        },
        "policies": {policy: _aggregate(items) for policy, items in sorted(policy_items.items())},
        "paired_delta_interval": interval,
        "changes": changes,
        "input_hashes_unchanged": all(sha256(source / name) == digest for name, digest in input_hashes.items()),
    }
    verification = verify(destination)
    result["verification"] = verification
    atomic_json(destination / "audit.json", result)

    report = destination / "REPORT.md"
    banner = [
        f"> 离线后处理审计：复用 {source.name} 的原始回答，新增模型调用 0 次；原运行文件未改动。",
        f"> 后处理版本 `{POSTPROCESS_VERSION}`；重放任务 {', '.join(sorted(REPLAY_TASKS))}；"
        f"罪名本体归一化 {'开启' if charge_canonicalization else '关闭'}。",
        f"> 均分 {original_summary['mean_score_all']:.2%} → {summary['mean_score_all']:.2%}"
        f"（{result['score_delta'] * 100:+.2f} 个百分点），{len(changes)} 题变化。",
        "> 后处理只使用题面与题面级规则，不读取参考答案；原预测、原分数与逐题变化均保存在 `audit.json`。",
        "", "",
    ]
    report.write_text("\n".join(banner) + report.read_text(encoding="utf-8"), encoding="utf-8")
    if not result["input_hashes_unchanged"] or not verification["valid"]:
        raise ValueError("Audit verification failed; inspect audit.json")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--charge-canonicalization", action="store_true",
        help="诊断选项：把 3-3 的本体外罪名按唯一超串映射回本体；属于评分口径放宽，须与严格分分列",
    )
    args = parser.parse_args()
    result = replay(args.source, args.destination, charge_canonicalization=args.charge_canonicalization)
    print(json.dumps(
        {key: value for key, value in result.items() if key not in {"changes", "verification"}},
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
