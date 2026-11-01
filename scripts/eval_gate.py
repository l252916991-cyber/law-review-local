"""Run the answer-quality release gate and compare it with the committed baseline.

Rules mode (CI, no model)::

    uv run --locked python scripts/eval_gate.py

Model mode against a local OpenAI-compatible server::

    uv run --locked python scripts/eval_gate.py --mode model \\
        --endpoint http://127.0.0.1:8000/v1 --model ACTUAL_MODEL_ID

Register a new baseline after an intended change (the reason is stored)::

    uv run --locked python scripts/eval_gate.py --update-baseline --reason "..."

Exit codes: 0 passed, 1 blocked by a regression, 2 configuration error.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.eval_gate import (  # noqa: E402
    DEFAULT_BASELINES,
    DEFAULT_DATASET,
    GateConfigurationError,
    baseline_from,
    compare,
    render_markdown,
    run,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("rules", "model"), default="rules")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--endpoint", help="model mode: loopback OpenAI-compatible base URL")
    parser.add_argument("--model", help="model mode: model id served by the endpoint")
    parser.add_argument("--postgres", metavar="ADMIN_URL",
                        help="run against a throwaway PostgreSQL database created through this URL")
    parser.add_argument("--report", type=Path, help="write the full JSON report here")
    parser.add_argument("--markdown", type=Path, help="append a Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    if args.mode == "model" and not (args.endpoint and args.model):
        parser.error("--mode model 需要 --endpoint 与 --model")
    baseline_path = args.baseline or DEFAULT_BASELINES[args.mode]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None

    try:
        with tempfile.TemporaryDirectory(prefix="lexvault-eval-gate-") as directory:
            report = run(args.mode, dataset_path=args.dataset.resolve(), workdir=Path(directory),
                         endpoint=args.endpoint, model=args.model, postgres_admin_url=args.postgres)
        verdict = compare(report, baseline) if baseline and not args.update_baseline else None
        if args.update_baseline:
            new = baseline_from(report, reason=args.reason, previous=baseline)
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except GateConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if verdict is not None:
        report["verdict"] = verdict
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(report, verdict, baseline)
    if args.markdown:
        with args.markdown.open("a", encoding="utf-8") as stream:
            stream.write(markdown)
    print(markdown)
    if args.update_baseline:
        print(f"已登记基线：{baseline_path.relative_to(ROOT) if baseline_path.is_relative_to(ROOT) else baseline_path}")
        return 0
    if baseline is None:
        print(f"没有基线 {baseline_path}；先用 --update-baseline --reason 登记。", file=sys.stderr)
        return 2
    return 0 if verdict and verdict["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
