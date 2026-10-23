"""Frozen 50-question screening, then 200 disjoint questions for qualifying modes."""
from __future__ import annotations

import argparse
import fcntl
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.correction_candidates import MODES, predict
from app.benchmark_metrics import score_lawbench_item
from app.benchmark_reporting import atomic_json
from app.lawbench import load_task
from scripts.benchmark_campaign import digest, probe_model, service_may_still_be_busy

CONFIG = dict(model="Qwythos-9B-v2-8bit-mlx", url="http://127.0.0.1:8000/v1", temperature=0.0,
              max_tokens=1600, timeout=180, enable_thinking=False, strategy="task_guided")


def run(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        data = load_task("2-1")
        indices = list(range(len(data)))
        random.Random(5092026).shuffle(indices)
        sources = ["app/correction_candidates.py", "scripts/correction_sweep.py", "app/benchmark_solver.py",
                   "app/benchmark_metrics.py", "app/benchmark_postprocess.py", "app/services.py", "app/config.py"]
        manifest: dict[str, Any] = {"config": CONFIG, "screen": indices[:50], "confirm": indices[50:250],
                    "modes": list(MODES), "threshold_points": 5,
                    "data_sha256": digest(ROOT / "benchmarks/lawbench/zero_shot/2-1.json"),
                    "sources": {name: digest(ROOT / name) for name in sources}}
        path = directory / "manifest.json"
        if path.exists():
            if json.loads(path.read_text()) != manifest:
                raise ValueError("Frozen configuration, data or code changed")
        else:
            atomic_json(path, manifest)
            for name in sources:
                target = directory / "source_snapshot" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
        probe_model(CONFIG)
        summaries: dict[str, Any] = {}

        def batch(stage: str, mode: str) -> dict[str, Any]:
            rows = []
            target = directory / stage / mode
            target.mkdir(parents=True, exist_ok=True)
            for index in manifest[stage]:
                item = data[index]
                checkpoint = target / f"{index:04d}.json"
                if checkpoint.exists():
                    row = json.loads(checkpoint.read_text())
                    if row["question"] != item["question"] or row["reference"] != item["answer"]:
                        raise ValueError("Checkpoint input mismatch")
                else:
                    result = predict(mode, item["instruction"], item["question"], CONFIG)
                    row = {"question_id": f"2-1_{index:04d}", "question": item["question"],
                           "instruction": item["instruction"], "reference": item["answer"], **result}
                    row["score"] = score_lawbench_item("2-1", result["prediction"], item["answer"], question=item["question"]).score if not result["error"] else 0
                    atomic_json(checkpoint, row)
                rows.append(row)
                print(f"{stage}/{mode} {len(rows)}/{len(manifest[stage])} score={row['score']:.4f} error={row['error']}", flush=True)
                if service_may_still_be_busy(row["error"]) or (row["error"] and "Connection refused" in row["error"]):
                    raise RuntimeError("Model service failed; results retained")
            summary = {"count": len(rows), "score": sum(r["score"] for r in rows) / len(rows) * 100,
                       "failed": sum(bool(r["error"]) for r in rows), "calls": sum(len(r["calls"]) for r in rows)}
            atomic_json(target / "summary.json", summary)
            summaries[f"{stage}/{mode}"] = summary
            atomic_json(directory / "summary.json", summaries)
            return summary

        baseline = batch("screen", "baseline")
        qualified = []
        for mode in MODES[1:]:
            result = batch("screen", mode)
            if result["score"] >= baseline["score"] + 5 and result["failed"] <= baseline["failed"]:
                qualified.append(mode)
        atomic_json(directory / "selection.json", {"qualified": qualified, "threshold_points": 5})
        if qualified:
            batch("confirm", "baseline")
            for mode in qualified:
                batch("confirm", mode)
        atomic_json(directory / "completed.json", {"completed": True, "qualified": qualified})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    run(parser.parse_args().directory)
