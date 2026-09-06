"""Read-only installation checks; never downloads models or sends case data."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sqlite3
import subprocess
import sys


PACKAGES = (
    "fastapi", "uvicorn", "python-multipart", "python-docx", "arq", "redis",
    "langgraph", "langgraph-checkpoint-sqlite", "jieba", "cn2an",
)
TOOLS = ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")


def inspect_environment() -> dict[str, object]:
    packages: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    with sqlite3.connect(":memory:") as database:
        try:
            database.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(text)")
            fts5 = True
        except sqlite3.OperationalError:
            fts5 = False
    tools = {name: shutil.which(name) for name in TOOLS}
    languages: list[str] = []
    if tools["tesseract"]:
        result = subprocess.run(
            [str(tools["tesseract"]), "--list-langs"], capture_output=True, text=True, timeout=10,
            check=False,
        )
        languages = [line.strip() for line in result.stdout.splitlines()[1:] if line.strip()]
    return {
        "python": sys.version.split()[0],
        "supported_python": (3, 11) <= sys.version_info[:2] < (3, 15),
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_fts5": fts5,
        "packages": packages,
        "document_tools": tools,
        "ocr_languages": languages,
        "ocr_ready": all(tools.values()) and {"chi_sim", "eng"}.issubset(languages),
        "model_server": "not probed (no network requests)",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-ocr", action="store_true", help="Fail when PDF/OCR tools or languages are missing")
    args = parser.parse_args()
    report = inspect_environment()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    packages = report["packages"]
    assert isinstance(packages, dict)
    ready = report["supported_python"] and report["sqlite_fts5"] and all(packages.values())
    return 0 if ready and (not args.require_ocr or report["ocr_ready"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
