"""Compare pinned existing local models on fresh synthetic seed data only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import db_scope, init_db, now, transaction
from app.local_backends import LocalModels
from app.model_comparison import compare_local_models, private_report

QUESTIONS = (
    ("ordinary", 1, "请梳理固定回报审批的事实与证据。", "success"),
    ("contradiction", 1, "核对固定回报审批矛盾。", "success"),
    ("gaps", 1, "核对固定回报审批矛盾和证据疏漏", "success"),
    ("unrelated-query", 1, "量子香蕉完全无关资料", "source_rejection"),
    ("empty-case", 2, "量子香蕉完全无关资料", "source_rejection"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="private host JSON: endpoint, identities, models")
    parser.add_argument("--framework-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, mode=0o700)
    values = json.loads(args.registry.read_text())
    models = LocalModels(**values)
    source = args.output / "synthetic-source" / "host.sqlite"
    reports = []
    with db_scope(source):
        init_db(seed=True)
        with transaction() as conn:
            conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('Synthetic empty comparison',?,?)", (now(), now()))
        for name, case_id, question, expected in QUESTIONS:
            report = compare_local_models(case_id=case_id, question=question, expected=expected, models=models,
                framework_python=args.framework_python, output=args.output / name)
            reports.append({"name": name, "engineering_passed": report["engineering_passed"],
                "candidate": report["candidate"]["status"], "source_identity": report["source_identity"]})
            print(json.dumps(reports[-1]), flush=True)
    private_report(args.output / "summary.json", {"material": "built-in synthetic seed only", "runs": reports,
        "measurement_valid": models.transport is None, "g2_accepted": False})
    return 0 if all(r["engineering_passed"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
