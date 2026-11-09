"""Compatibility CLI for the current isolated Playwright UI acceptance flows.

The legacy DOM/CDP flows are superseded by frontend/e2e. This entry point keeps
the flow names, but every reported result now comes from the current UI and a
fresh FastAPI process with synthetic data and model test doubles.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLOW_PATTERNS = {
    "metrics": "background agent",
    "chat": "streamed answer|network retry",
    "agent": "background agent|failed node",
    "workbench": "background agent|failed node|evidence editing",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", choices=tuple(FLOW_PATTERNS), default="metrics")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/browser-check")
    parser.add_argument("--browser", help="Chromium-family executable; otherwise use Playwright Chromium")
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.data_dir is not None:
        parser.error("新版验收始终新建隔离合成数据，不复用 --data-dir")
    pnpm = shutil.which("pnpm")
    if pnpm is None or shutil.which("node") is None:
        parser.error("请先将 frontend/.node-version 指定的 Node 和 pnpm 加入 PATH")
    if not (ROOT / "frontend/out/index.html").is_file():
        parser.error("请先执行 pnpm --dir frontend build:static")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    browser = args.browser or environment.get("LEXVAULT_BROWSER_PATH") or environment.get("LAW_REVIEW_BROWSER")
    if browser:
        if not Path(browser).is_file():
            parser.error("指定的浏览器可执行文件不存在")
        environment["LEXVAULT_BROWSER_PATH"] = browser
    environment["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(output / "playwright.json")
    result = subprocess.run([
        pnpm, "exec", "playwright", "test", "--grep", FLOW_PATTERNS[args.flow],
        "--output", str(output / "artifacts"), "--reporter", "line,json",
    ], cwd=ROOT / "frontend", env=environment)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True)
    raw = output / "playwright.json"
    details = json.loads(raw.read_text()) if raw.is_file() else {}
    report = {
        "ok": result.returncode == 0, "flow": args.flow, "model": "stub",
        "runner": "Playwright against FastAPI static UI", "browser": browser or "Playwright Chromium",
        "source_head": revision.stdout.strip() if revision.returncode == 0 else "unavailable",
        "stats": details.get("stats", {}), "native_report": str(raw),
        "counts_compatible_with_legacy_flows": False,
    }
    (output / "browser-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
