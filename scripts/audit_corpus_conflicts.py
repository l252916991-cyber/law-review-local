"""Audit duplicate statute publications across corpus directories; read-only.

Cross-source governance gate before merging corpus directories into one canonical
corpus: classifies each duplicate ``(law_name, version_date)`` key as exact /
cosmetic / boundary / substantive and never rewrites corpus data. Runs anywhere,
so a newly added law directory is audited by re-running the same command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.corpus_audit import ACTIONS, audit  # noqa: E402

SEVERITY = {kind: rank for rank, kind in enumerate(
    ("substantive_conflict", "boundary_difference", "cosmetic_duplicate", "exact_duplicate"))}
assert set(SEVERITY) == set(ACTIONS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, action="append", default=None, help="可重复：法条库目录")
    parser.add_argument("--root", type=Path, help="扫描该目录下所有含 manifest.json 的直接子目录")
    parser.add_argument("--output", type=Path, help="把完整报告写成 JSON")
    args = parser.parse_args(argv)
    directories = [str(directory) for directory in (args.corpus_dir or [])]
    if args.root:
        directories += sorted(str(path) for path in args.root.iterdir() if (path / "manifest.json").exists())
    if not directories:
        parser.error("至少需要一个 --corpus-dir 或 --root")
    report = audit(directories)
    # Confront the cases needing human action first.
    ordered = {**report, "conflicts": sorted(
        report["conflicts"], key=lambda record: (SEVERITY[record["difference_type"]], record["law_name"]))}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(ordered, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
