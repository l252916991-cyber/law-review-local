"""Export one ablation group's answers into the answer-judge input format.

The ledger keeps the model's raw answer and the frozen contexts; the judge needs
``{id: {answer, citations, answer_status}}``. Citations are derived exactly the
way the driver scores them, so the judge sees the same citation set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.evaluation.collaboration_ablation import answer_citations  # noqa: E402

EXIT_PROTOCOL = 2


def export(batch: Path, group: str, *, repeat: int = 0) -> dict[str, Any]:
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    ledger_path = batch / "run" / "attempts.jsonl"
    ledger_bytes = ledger_path.read_bytes()
    ledger = [json.loads(line) for line in ledger_bytes.decode("utf-8").splitlines() if line.strip()]
    rows = {}
    for row in ledger:
        key = (row["item"], int(row.get("repeat", 0)), row["group"])
        if key in rows:
            raise ValueError(f"重复的题目/轮次/分组记录：{key}")
        rows[key] = row
    answers: dict[str, Any] = {}
    for entry in manifest["items"]:
        item_id = entry["id"]
        prepared = json.loads((batch / "items" / f"{item_id}.json").read_text(encoding="utf-8"))
        row = rows.get((item_id, repeat, group))
        if row is None:
            raise ValueError(f"{item_id}/{group} 在第 {repeat} 轮没有记录")
        text = str(row.get("raw_answer") or "")
        answers[item_id] = {
            "answer": text,
            "citations": answer_citations(text, prepared["contexts"]),
            "answer_status": "insufficient_evidence" if row.get("score", {}).get("refused") else "answered",
            # Preserve the recorded outcome separately from the historical
            # answer-status contract: empty/failed attempts are not successes.
            "outcome": row.get("outcome"),
            "executed": row.get("executed"),
            "passed": row.get("passed"),
            "repeat": row.get("repeat"),
            "input_tokens": row.get("input_tokens"),
            "output_tokens": row.get("output_tokens"),
            "latency_ms": row.get("latency_ms"),
            "elapsed_seconds": row.get("elapsed_seconds"),
            "usage_known": row.get("usage_known"),
        }
    return {"collection_mode": f"real_model_ablation_{group}", "group": group, "repeat": repeat,
            "scoring_version": 2, "dataset": manifest["dataset"]["name"],
            "attempts_sha256": hashlib.sha256(ledger_bytes).hexdigest(), "answers": answers}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出某一组的三组对照答案供 Judge 使用")
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--group", required=True, choices=("A", "B", "C"))
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = export(args.batch, args.group, repeat=args.repeat)
    except (OSError, ValueError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return EXIT_PROTOCOL
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"output": str(args.output), "group": args.group,
                      "items": len(payload["answers"]), "dataset": payload["dataset"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
