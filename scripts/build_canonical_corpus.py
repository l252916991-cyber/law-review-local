"""Build one canonical statute corpus from many builder output directories.

Merges overlapping ``legal_corpus*`` directories into a single corpus with exactly one
document per ``(law_name, version_date)``. Selects a source copy verbatim; never edits
legal text. Refuses to build if the read-only audit finds a substantive conflict, so a
real text disagreement is never silently resolved.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.corpus_canonical import build  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, action="append", default=None, help="可重复：来源法条库目录")
    parser.add_argument("--root", type=Path, help="扫描该目录下所有含 manifest.json 的直接子目录")
    parser.add_argument("--output", type=Path, required=True, help="canonical 语料目录")
    args = parser.parse_args(argv)
    directories = [str(directory) for directory in (args.corpus_dir or [])]
    if args.root:
        directories += sorted(str(path) for path in args.root.iterdir() if (path / "manifest.json").exists())
    if not directories:
        parser.error("至少需要一个 --corpus-dir 或 --root")
    report = build(directories, args.output)
    print(json.dumps({key: value for key, value in report.items() if key != "documents"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
