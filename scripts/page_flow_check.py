"""Verify real page flows in a headless browser against a temporary server.

The checks run against a freshly seeded synthetic demo case in an isolated data
directory, with a real FastAPI process and a real Chromium-family browser. Nothing
here touches real case material, and no model endpoint is contacted: the flows
exercise the usage panel and the request failure/empty states, which do not need
model output.

Usage::

    UV_CACHE_DIR=/tmp/uv-cache uv run --locked python scripts/page_flow_check.py \
        --flow metrics --output-dir output/browser-check

Exit code is non-zero when a flow step reports a failure. A host without a
supported browser reports "browser unavailable" instead of pretending to pass.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Shared helpers injected into every step. Everything the assertions read comes
# from the rendered DOM, never from module internals.
PRELUDE = r"""
const failures = [];
const notes = [];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const text = (selector) => { const node = document.querySelector(selector); return node ? node.textContent.trim() : null; };
const html = (selector) => { const node = document.querySelector(selector); return node ? node.innerHTML : ""; };
const rows = (selector) => Array.from(document.querySelectorAll(selector + " .usage-row:not(.usage-head)")).map((row) => row.textContent.replace(/\s+/g, " ").trim());
const expect = (label, actual, wanted) => {
  if (actual !== wanted) failures.push(`${label}：期望 ${JSON.stringify(wanted)}，实际 ${JSON.stringify(actual)}`);
};
const expectMatch = (label, actual, pattern) => {
  if (!pattern.test(String(actual))) failures.push(`${label}：不匹配 ${pattern}，实际 ${JSON.stringify(actual)}`);
};
const waitFor = async (label, predicate, timeout = 10000) => {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    let value = false;
    try { value = Boolean(predicate()); } catch (error) { failures.push(`${label}：${error.message}`); return false; }
    if (value) return true;
    await sleep(60);
  }
  failures.push(`${label}：等待超时`);
  return false;
};
"""

BODY_OPEN_METRICS = r"""  await waitFor("案件列表加载", () => document.querySelector(".case-item"));
  const caseButton = document.querySelector(".case-item");
  if (!caseButton) { failures.push("案件列表为空"); return {failures, notes, case_id: null}; }
  const caseId = Number(caseButton.dataset.caseId);
  caseButton.click();
  await waitFor("案件标题", () => (text("#case-title") || "").includes("合成演示"));
  document.querySelector('.nav-item[data-view="lab"]').click();
  await waitFor("用量面板返回数据", () => (text("#usage-scope") || "").includes("次调用"));
  return {failures, notes, case_id: caseId};
"""

BODY_PARTIAL = r"""  await waitFor("面板仍显示本案调用", () => (text("#usage-scope") || "").includes("次调用"));
  expect("调用数", text("#usage-calls"), "3");
  expect("失败数", text("#usage-failed"), "失败 1 次");
  expect("输入 tokens 为已知下界", text("#usage-input-tokens"), "≥30");
  expect("输入 tokens 覆盖说明", text("#usage-input-note"), "已知部分 · 2/3 次调用有 usage");
  expect("输出 tokens", text("#usage-output-tokens"), "≥10");
  expect("首 token P50", text("#usage-ttft-p50"), "50ms");
  expect("首 token P95", text("#usage-ttft-p95"), "59ms");
  expect("首 token 样本数", text("#usage-ttft-note"), "可用样本 2/3");
  expect("调用延迟 P50", text("#usage-latency-p50"), "200ms");
  expect("调用延迟 P95", text("#usage-latency-p95"), "290ms");
  const kinds = rows("#usage-by-kind");
  if (kinds.length !== 2) failures.push(`按类型分组应有 2 行，实际 ${kinds.length}`);
  expectMatch("按类型分组含 chat", kinds.join(" | "), /chat/);
  expectMatch("按类型分组含 critic", kinds.join(" | "), /critic/);
  expectMatch("按类型分组标注覆盖", kinds.join(" | "), /≥|未知/);
  const models = rows("#usage-by-model");
  if (models.length !== 2) failures.push(`按模型分组应有 2 行，实际 ${models.length}`);
  expectMatch("按模型分组含模型名", models.join(" | "), /local-a/);
  return {failures, notes, rows: {kinds, models}};
"""

BODY_EMPTY_CASE = r"""  const created = await (await fetch("/api/cases", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({title: "空用量案件（流程验证）"}),
  })).json();
  window.__emptyCaseId = created.id;
  await selectCase(created.id);
  await waitFor("空案件面板", () => text("#usage-scope") === "尚无调用");
  expect("空案件调用数", text("#usage-calls"), "0");
  expect("空案件输入 tokens", text("#usage-input-tokens"), "—");
  expect("空案件输入说明", text("#usage-input-note"), "尚无调用");
  expect("空案件延迟", text("#usage-latency-p50"), "—");
  expectMatch("空案件分组", html("#usage-by-kind"), /尚无调用记录/);
  return {failures, notes, empty_case_id: created.id};
"""

BODY_RAPID_SWITCH = r"""  const demoId = window.__demoCaseId;
  const emptyId = window.__emptyCaseId;
  // Two switches without awaiting the first: the late answer must not land.
  selectCase(demoId);
  selectCase(emptyId);
  await waitFor("切回空案件", () => text("#usage-scope") === "尚无调用");
  await sleep(1500);
  expect("快速切换后仍为空案件", text("#usage-scope"), "尚无调用");
  expect("快速切换后调用数", text("#usage-calls"), "0");
  expect("快速切换后输入 tokens", text("#usage-input-tokens"), "—");
  return {failures, notes};
"""

BODY_FAILURE = r"""  const demoId = window.__demoCaseId;
  const realFetch = window.fetch;
  window.__realFetch = realFetch;
  window.fetch = (input, init) => String(input).includes("platform-metrics")
    ? Promise.reject(new Error("流程验证：模拟指标接口失败"))
    : realFetch(input, init);
  await selectCase(demoId);
  await waitFor("指标不可用状态", () => text("#usage-scope") === "指标不可用");
  expect("失败时输入 tokens 已清空", text("#usage-input-tokens"), "—");
  expect("失败时调用数已清空", text("#usage-calls"), "—");
  expectMatch("失败原因可见", text("#usage-failed"), /模拟指标接口失败|请求失败/);
  expectMatch("失败提示", html("#usage-by-kind"), /加载失败/);
  return {failures, notes};
"""

BODY_RECOVERY = r"""  window.fetch = window.__realFetch;
  await selectCase(window.__demoCaseId);
  await waitFor("恢复后重新显示用量", () => (text("#usage-scope") || "").includes("次调用"));
  expect("恢复后调用数", text("#usage-calls"), "3");
  expect("恢复后输入 tokens", text("#usage-input-tokens"), "≥30");
  return {failures, notes};
"""


def _script(body: str) -> str:
    """Wrap one step in its own async function so helper names are step-scoped."""
    return "(async () => {\n" + PRELUDE + body + "\n})()"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(base_url: str, process: subprocess.Popen[bytes], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("the application server exited before answering")
        try:
            with _NO_PROXY.open(f"{base_url}/api/health", timeout=2) as response:
                if response.status < 500:
                    return
        except OSError:
            time.sleep(0.3)
    raise TimeoutError("the application server did not become healthy")


def _seed_demo(data_dir: Path) -> None:
    environment = {**os.environ, "LAW_REVIEW_DATA_DIR": str(data_dir)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/seed_demo_case.py"), "--data-dir", str(data_dir)],
        cwd=ROOT, env=environment, text=True, capture_output=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"seeding the demo case failed: {result.stdout[-800:]}{result.stderr[-800:]}")


def _seed_usage(data_dir: Path, case_id: int) -> None:
    """Three calls: two with usage (one failed), one without; TTFT on two.

    The numbers are chosen so the panel's percentiles are distinguishable:
    TTFT 40/60 -> P50 50, P95 59; latency 100/200/300 -> P50 200, P95 290.
    """
    os.environ["LAW_REVIEW_DATA_DIR"] = str(data_dir)
    from app.db import db_scope
    from app.llm_usage import record_llm_call

    with db_scope(data_dir / "law_review.db"):
        record_llm_call(case_id=case_id, kind="chat", model="local-a",
                        usage={"prompt_tokens": 10, "completion_tokens": 4},
                        latency_ms=100, ttft_ms=40)
        record_llm_call(case_id=case_id, kind="chat", model="local-a",
                        usage=None, latency_ms=300)
        record_llm_call(case_id=case_id, kind="critic", model="local-b",
                        usage={"prompt_tokens": 20, "completion_tokens": 6},
                        latency_ms=200, ttft_ms=60, status="failed")


def _api_json(base_url: str, path: str) -> Any:
    with _NO_PROXY.open(f"{base_url}{path}", timeout=10) as response:
        return json.loads(response.read())


def _run_step(session: Any, name: str, script: str, output_dir: Path, shot: bool) -> dict[str, Any]:
    result = session.evaluate(script)
    failures = list(result.get("failures") or []) if isinstance(result, dict) else ["步骤未返回结果"]
    if shot:
        session.screenshot(output_dir / f"{name}.png")
        try:
            session.screenshot(output_dir / f"{name}-panel.png", selector="#usage-panel")
        except RuntimeError:
            pass
    for failure in failures:
        print(f"  [FAIL] {name}: {failure}", file=sys.stderr)
    if not failures:
        print(f"  [ok] {name}")
    return {"step": name, "failures": failures, "result": result}


FLOWS = ("metrics",)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", default="metrics", choices=FLOWS)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "browser-check")
    parser.add_argument("--data-dir", type=Path, help="reuse an existing isolated data directory")
    parser.add_argument("--browser", help="explicit browser executable")
    args = parser.parse_args()

    from scripts.browser_driver import BrowserSession, find_browser

    browser = find_browser(args.browser)
    if not browser:
        print(json.dumps({"ok": False, "reason": "browser unavailable"}, ensure_ascii=False))
        return 2

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.data_dir:
        data_dir = args.data_dir
        if data_dir.exists():
            shutil.rmtree(data_dir)
        data_dir.mkdir(parents=True)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="lexvault-page-flow-")
        data_dir = Path(temporary.name)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "LAW_REVIEW_DATA_DIR": str(data_dir),
        "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(data_dir / "checkpoints.sqlite"),
        "LAW_REVIEW_AUTH_MODE": "local",
        "LAW_REVIEW_LLM_URL": "http://127.0.0.1:1/v1",
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }
    log_path = output_dir / "server.log"
    steps: list[dict[str, Any]] = []
    session = None
    process = None
    try:
        print(f"[1/4] 准备隔离数据目录 {data_dir}")
        _seed_demo(data_dir)
        _seed_usage(data_dir, 1)
        print(f"[2/4] 启动应用服务 {base_url}")
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=environment, stdout=log, stderr=log,
            )
            _wait_for_server(base_url, process)
            cases = _api_json(base_url, "/api/cases")
            if not cases:
                raise RuntimeError("the seeded demo case is missing")
            demo_id = int(cases[0]["id"])
            usage = _api_json(base_url, f"/api/cases/{demo_id}/platform-metrics")["llm_usage"]
            if usage["calls"] != 3:
                raise RuntimeError(f"unexpected seeded usage: {usage}")
            print(f"[3/4] 启动真实浏览器（{Path(browser).name}）执行 {args.flow} 流程")
            with BrowserSession(base_url + "/", browser=browser) as opened:
                session = opened
                session.wait_until("document.readyState === 'complete'")
                session.evaluate(f"window.__demoCaseId = {demo_id}; window.__emptyCaseId = null;")
                steps.append(_run_step(session, "00-open", _script(BODY_OPEN_METRICS), output_dir, False))
                steps.append(_run_step(session, "01-partial", _script(BODY_PARTIAL), output_dir, True))
                steps.append(_run_step(session, "02-empty", _script(BODY_EMPTY_CASE), output_dir, True))
                steps.append(_run_step(session, "03-rapid-switch", _script(BODY_RAPID_SWITCH), output_dir, True))
                steps.append(_run_step(session, "04-failure", _script(BODY_FAILURE), output_dir, True))
                steps.append(_run_step(session, "05-recovery", _script(BODY_RECOVERY), output_dir, True))
    finally:
        if session is not None:
            session.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()

    failures = [failure for step in steps for failure in step["failures"]]
    report = {
        "ok": not failures,
        "flow": args.flow,
        "base_url": base_url,
        "browser": browser,
        "source_head": _source_head(),
        "steps": [{key: step[key] for key in ("step", "failures")} for step in steps],
        "failures": failures,
        "screenshots": sorted(path.name for path in output_dir.glob("*.png")),
        "server_log": str(log_path),
    }
    (output_dir / "browser-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[4/4] {'通过' if not failures else '失败'}，报告：{output_dir / 'browser-check.json'}")
    if temporary is not None:
        temporary.cleanup()
    return 0 if not failures else 1


def _source_head() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
