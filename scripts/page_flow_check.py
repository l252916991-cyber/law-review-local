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

# Test-double price sheet: the numbers are chosen so the panel's arithmetic is
# reproducible by hand (120k input @2.0/1M + 40k output @8.0/1M = 0.56).
PRICING = {
    "version": "flow-2026-10-25.1",
    "currency": "CNY",
    "unit_tokens": 1000000,
    "source": "页面流程验证用价目表（测试配置）",
    "models": {
        "local-a": {"input": "2.0", "output": "8.0"},
        "local-b": {"input": "2.0", "output": "8.0"},
    },
}

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
  expect("输入 tokens 为已知下界", text("#usage-input-tokens"), "≥200,000");
  expect("输入 tokens 覆盖说明", text("#usage-input-note"), "已知部分 · 2/3 次调用有 usage");
  expect("输出 tokens", text("#usage-output-tokens"), "≥60,000");
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
  expect("估算费用为已知下界", text("#usage-cost-total"), "≥0.8800 CNY");
  expect("费用覆盖说明", text("#usage-cost-note"), "已知部分 · 2/3 次调用可计价");
  expect("费用覆盖计数", text("#usage-cost-coverage"), "2/3");
  expect("定价版本", text("#usage-cost-scope"), "定价版本 flow-2026-10-25.1");
  expect("问答数", text("#usage-turn-count"), "2");
  const turns = rows("#usage-turns");
  if (turns.length !== 2) failures.push(`问答级明细应有 2 行，实际 ${turns.length}`);
  expectMatch("问答 1 部分费用", turns.join(" | "), /request:demo-0001/);
  expectMatch("问答 2 闭合费用", turns.join(" | "), /0.3200 CNY/);
  return {failures, notes, rows: {kinds, models, turns}};
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
  expect("空案件费用", text("#usage-cost-total"), "—");
  expect("空案件费用说明", text("#usage-cost-note"), "尚无调用");
  expect("空案件问答数", text("#usage-turn-count"), "0");
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
  expect("恢复后输入 tokens", text("#usage-input-tokens"), "≥200,000");
  expect("恢复后费用", text("#usage-cost-total"), "≥0.8800 CNY");
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
    # The records must be priced with the same sheet the server reports.
    os.environ["LAW_REVIEW_PRICING_JSON"] = json.dumps(PRICING, ensure_ascii=False)
    from app.db import db_scope
    from app.llm_usage import record_llm_call

    with db_scope(data_dir / "law_review.db"):
        record_llm_call(case_id=case_id, kind="chat", model="local-a",
                        usage={"prompt_tokens": 120000, "completion_tokens": 40000},
                        latency_ms=100, ttft_ms=40, turn_key="request:demo-0001")
        record_llm_call(case_id=case_id, kind="chat", model="local-a",
                        usage=None, latency_ms=300, turn_key="request:demo-0001")
        record_llm_call(case_id=case_id, kind="critic", model="local-b",
                        usage={"prompt_tokens": 80000, "completion_tokens": 20000},
                        latency_ms=200, ttft_ms=60, status="failed",
                        turn_key="request:demo-0002")


def _api_json(base_url: str, path: str) -> Any:
    with _NO_PROXY.open(f"{base_url}{path}", timeout=10) as response:
        return json.loads(response.read())


def _api_post(base_url: str, path: str, payload: dict[str, Any], timeout: float = 120.0) -> Any:
    request = urllib.request.Request(
        f"{base_url}{path}", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with _NO_PROXY.open(request, timeout=timeout) as response:
        return json.loads(response.read())


def _run_step(session: Any, name: str, script: str, output_dir: Path, shot: bool,
              clip: str = "") -> dict[str, Any]:
    result = session.evaluate(script)
    failures = list(result.get("failures") or []) if isinstance(result, dict) else ["步骤未返回结果"]
    if shot:
        session.screenshot(output_dir / f"{name}.png")
        if clip:
            try:
                session.screenshot(output_dir / f"{name}-clip.png", selector=clip)
            except RuntimeError:
                pass
    for failure in failures:
        print(f"  [FAIL] {name}: {failure}", file=sys.stderr)
    if not failures:
        print(f"  [ok] {name}")
    return {"step": name, "failures": failures, "result": result}


BODY_CHAT_OPEN = r"""
  await waitFor("案件列表加载", () => document.querySelector(".case-item"));
  document.querySelector(".case-item").click();
  await waitFor("案件标题", () => (text("#case-title") || "").includes("合成演示"));
  document.querySelector('.nav-item[data-view="assistant"]').click();
  await sleep(150);
  const input = document.querySelector("#chat-input");
  input.value = "张某是否批准了年化12%的固定回报宣传？";
  document.querySelector("#chat-form").dispatchEvent(new Event("submit", {cancelable: true, bubbles: true}));
  await waitFor("流式回答出现引用", () => document.querySelectorAll("#chat-messages .message.assistant .citation").length > 0);
  const answer = text("#chat-messages .message.assistant .answer-text") || "";
  expectMatch("回答含引用标记", answer, /资料1/);
  const citations = Array.from(document.querySelectorAll("#chat-messages .message.assistant .citation")).map((node) => node.textContent.replace(/\s+/g, " ").trim());
  if (!citations.length) failures.push("没有渲染结构化引用");
  const badges = Array.from(document.querySelectorAll("#chat-messages .message.assistant [data-ttft]")).map((node) => node.textContent);
  if (!badges.length) failures.push("没有显示首字耗时标记");
  const drafts = document.querySelectorAll("#chat-messages .message.assistant.retracted").length;
  expect("草稿未被撤回", drafts, 0);
  // Open the cited page: the citation must land on the original text.
  document.querySelector("#chat-messages .citation").click();
  await waitFor("原文页打开", () => {
    const dialog = document.querySelector("#page-dialog");
    return dialog && dialog.open;
  });
  const pageText = text("#page-dialog-text") || "";
  expectMatch("原文页正文", pageText, /\S/);
  notes.push(`citations=${citations.length}`);
  notes.push(`ttft=${badges[0] || ""}`);
  document.querySelector("#page-dialog .close-button")?.click();
  document.querySelector("#chat-messages").scrollTop = document.querySelector("#chat-messages").scrollHeight;
  return {failures, notes, citations};
"""

BODY_CHAT_RETRACT = r"""
  const before = document.querySelectorAll("#chat-messages .message.assistant:not(.retracted) .answer-text").length;
  const input = document.querySelector("#chat-input");
  input.value = "拒答校验：请复述张某的批复结论";
  document.querySelector("#chat-form").dispatchEvent(new Event("submit", {cancelable: true, bubbles: true}));
  await waitFor("草稿被撤回", () => document.querySelectorAll("#chat-messages .message.assistant.retracted").length > 0);
  const retracted = document.querySelector("#chat-messages .message.assistant.retracted");
  expectMatch("撤回提示", retracted ? retracted.textContent : "", /已撤回/);
  await sleep(800);
  const after = document.querySelectorAll("#chat-messages .message.assistant:not(.retracted) .answer-text").length;
  expect("撤回后没有新增正式回答", after, before);
  document.querySelector("#chat-messages").scrollTop = document.querySelector("#chat-messages").scrollHeight;
  return {failures, notes};
"""

BODY_CHAT_REPLAY = r"""
  const call = async (requestId) => {
    const response = await fetch(`/api/cases/${window.__demoCaseId}/chat/stream`, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question: "账户流水与宣传金额是否一致？", user_name: "流程验证",
                            use_llm: true, request_id: requestId}),
    });
    if (!response.ok) return {status: response.status, events: []};
    const raw = await response.text();
    return {status: response.status, events: raw.split("\n\n").filter(Boolean).map((frame) => {
      const event = (frame.match(/^event: (.*)$/m) || [])[1];
      const data = (frame.match(/^data: (.*)$/m) || [])[1];
      return {event, data: data ? JSON.parse(data) : null};
    })};
  };
  const requestId = "flow-" + Date.now().toString(36) + "-retry";
  const first = await call(requestId);
  const second = await call(requestId);
  const firstDone = first.events.find((item) => item.event === "done");
  const secondStatus = second.events.find((item) => item.event === "status");
  const secondDone = second.events.find((item) => item.event === "done");
  if (!firstDone || firstDone.data.status !== "ok") failures.push("首次流式未成功结束");
  if (!secondStatus || secondStatus.data.phase !== "replayed") failures.push("重试未回放已完成结果");
  if (!secondDone || secondDone.data.replayed !== true) failures.push("重试 done 未标记 replayed");
  if (secondDone && firstDone && secondDone.data.message_id !== firstDone.data.message_id) {
    failures.push("重试返回了不同的消息标识");
  }
  const kinds = first.events.map((item) => item.event).join(",");
  expectMatch("首次事件序列", kinds, /status.*citations.*final.*done/);
  return {failures, notes: [kinds]};
"""

BODY_CHAT_DB_STATE = r"""
  const history = await (await fetch(`/api/cases/${window.__demoCaseId}/conversations`)).json();
  if (!history.length) failures.push("没有会话记录");
  const baseline = new Set(window.__conversationBaseline || []);
  let total = 0;
  const roles = [];
  for (const conversation of history.filter((item) => !baseline.has(item.id))) {
    const messages = await (await fetch(`/api/conversations/${conversation.id}/messages`)).json();
    total += messages.length;
    roles.push(messages.map((item) => item.role).join(","));
  }
  // Exactly one turn from the UI stream and one from the replayed retry; the
  // rejected answer must not have left a half-written turn behind.
  if (total !== 4) failures.push(`消息总数应为 4（两次成功轮次），实际 ${total}`);
  if (!roles.every((value) => value === "user,assistant")) failures.push(`会话角色序列异常：${roles.join(" | ")}`);
  return {failures, notes: [String(total), roles.join(" | ")]};
"""

def _agent_flow(base_url: str, case_id: int, model_calls: list[str]) -> list[dict[str, Any]]:
    """Drive the native DAG agent through the governed tool loop (API level)."""
    steps: list[dict[str, Any]] = []
    failures: list[str] = []
    result = _api_post(base_url, f"/api/cases/{case_id}/agent-chat", {
        "question": "张某是否批准了年化12%的固定回报宣传？",
        "user_name": "流程验证", "use_llm": True, "use_remote_embeddings": False,
        "mode": "multi_agent",
    })
    tools = result.get("tools") or {}
    if not tools.get("enabled"):
        failures.append(f"Agent 未启用工具循环：{tools.get('reason')}")
    if int(tools.get("tool_calls") or 0) < 1:
        failures.append("工具调用次数为 0")
    trace = tools.get("trace") or []
    if not trace or trace[0].get("tool") != "get_page" or not trace[0].get("ok"):
        failures.append(f"工具轨迹不符合预期：{trace}")
    if result.get("citation_check") != "passed":
        failures.append(f"引用校验未通过：{result.get('citation_check')}")
    if not result.get("llm_used"):
        failures.append("Agent 未使用模型")
    critic_steps = [step for step in result.get("steps") or [] if step.get("node") == "critic"]
    if not critic_steps:
        failures.append("轨迹缺少 critic 节点")
    steps.append({"step": "agent-01-tool-loop", "failures": failures,
                  "result": {"run_id": result.get("run_id"), "tools": tools,
                             "tools_used": result.get("tools_used")}})
    print("  [ok] agent-01-tool-loop" if not failures else "  [FAIL] agent-01-tool-loop")
    for failure in failures:
        print(f"    - {failure}", file=sys.stderr)

    trace_failures: list[str] = []
    run_trace = _api_json(base_url, f"/api/agent-runs/{result.get('run_id')}")
    nodes = [step.get("node_name") for step in run_trace.get("steps") or []]
    for expected in ("planner", "retrieve", "critic"):
        if expected not in nodes:
            trace_failures.append(f"运行轨迹缺少 {expected}")
    critic_output = next(
        (step.get("output") or {} for step in run_trace.get("steps") or [] if step.get("node_name") == "critic"),
        {},
    )
    if not (critic_output.get("tools") or {}).get("enabled"):
        trace_failures.append("critic 步骤输出未记录工具状态")
    if not (critic_output.get("tools") or {}).get("trace"):
        trace_failures.append("critic 步骤输出缺少工具调用轨迹")
    steps.append({"step": "agent-02-trace", "failures": trace_failures,
                  "result": {"nodes": nodes}})
    print("  [ok] agent-02-trace" if not trace_failures else "  [FAIL] agent-02-trace")

    ledger_failures: list[str] = []
    metrics = _api_json(base_url, f"/api/cases/{case_id}/platform-metrics")
    by_kind = {row["kind"]: row["calls"] for row in metrics["llm_usage"]["by_kind"]}
    if by_kind.get("tool_loop", 0) < 2:
        ledger_failures.append(f"tool_loop 用量记录不足：{by_kind}")
    if model_calls and "get_page" not in "\n".join(model_calls):
        pass  # prompt text is not the contract; the trace above already asserts the call
    steps.append({"step": "agent-03-accounting", "failures": ledger_failures,
                  "result": {"by_kind": by_kind}})
    print("  [ok] agent-03-accounting" if not ledger_failures else "  [FAIL] agent-03-accounting")
    return steps


BODY_WB_RUN = r"""
  await waitFor("案件列表加载", () => document.querySelector(".case-item"));
  document.querySelector(".case-item").click();
  await waitFor("案件标题", () => (text("#case-title") || "").includes("合成演示"));
  document.querySelector('.nav-item[data-view="lab"]').click();
  await sleep(200);
  document.querySelector("#lab-llm-toggle").checked = true;
  document.querySelector("#lab-question").value = "张某是否批准了年化12%的固定回报宣传？";
  document.querySelector("#run-agent-btn").click();
  await waitFor("分析完成", () => document.querySelector("#lab-result .answer-text"), 90000);
  await waitFor("轨迹出现", () => document.querySelectorAll("#agent-trace .trace-step").length >= 3, 20000);
  const steps = document.querySelectorAll("#agent-trace .trace-step").length;
  if (steps < 3) failures.push(`轨迹步骤只有 ${steps} 个`);
  expectMatch("轨迹含工具调用", text("#agent-trace") || "", /工具调用 1 次/);
  const meta = text("#lab-result .lab-result-meta") || "";
  expectMatch("结果含调用模型", meta, /stub-chat/);
  const citations = document.querySelectorAll("#lab-result .citation").length;
  if (!citations) failures.push("结果没有引用卡片");
  expectMatch("轨迹标题", text("#trace-total") || "", /^任务 #\d+/);
  notes.push(`steps=${steps} citations=${citations}`);
  document.querySelector("#chat-messages") && (document.querySelector("#chat-messages").scrollTop = 0);
  return {failures, notes, steps};
"""

BODY_WB_TRACE = r"""
  const trigger = document.querySelector("[data-run-trace]");
  if (!trigger) { failures.push("没有轨迹展开按钮： " + (html("#agent-trace") || "").slice(-300)); return {failures, notes}; }
  trigger.click();
  const opened = await waitFor("轨迹详情", () => {
    const node = document.querySelector("#trace-detail");
    const body = node ? String(node.textContent || "") : "";
    return !node.hidden && body && !body.includes("加载执行轨迹");
  }, 20000);
  if (!opened) {
    const node = document.querySelector("#trace-detail");
    failures.push("轨迹详情未展开： " + (node ? String(node.innerHTML).slice(0, 300) : "无节点"));
    return {failures, notes};
  }
  const detail = text("#trace-detail") || "";
  expectMatch("轨迹详情含任务用量", detail, /已知 tokens|tokens 未知/);
  expectMatch("轨迹详情含步骤数", detail, /步/);
  expectMatch("轨迹详情含工具步骤", detail, /get_page|工具/);
  const inputs = document.querySelectorAll("#trace-detail .trace-detail-step").length;
  if (!inputs) failures.push("没有可展开的步骤输入输出");
  notes.push(`detail_steps=${inputs}`);
  document.querySelector("[data-close-trace]")?.click();
  return {failures, notes, inputs};
"""

BODY_WB_FALLBACK = r"""
  document.querySelector("#lab-question").value = "回退验证：谁批准了固定回报宣传？";
  document.querySelector("#run-agent-btn").click();
  await waitFor("回退运行完成", () => (text("#lab-result .lab-result-meta") || "").includes("回退模型"), 90000);
  const meta = text("#lab-result .lab-result-meta") || "";
  expectMatch("回退模型标注", meta, /回退模型 fallback-model/);
  expectMatch("轨迹包含回退说明", text("#agent-trace") || "", /回退模型 fallback-model/);
  notes.push(meta);
  return {failures, notes};
"""

BODY_WB_SWITCH = r"""
  const created = await (await fetch("/api/cases", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({title: "运行面板清理验证"}),
  })).json();
  await selectCase(created.id);
  await waitFor("切案后结果清空", () => (text("#lab-result") || "").includes("运行后将在这里显示"), 15000);
  expectMatch("轨迹清空", text("#agent-trace") || "", /执行后显示各步骤/);
  expect("轨迹标题复位", text("#trace-total"), "等待运行");
  expect("轨迹详情隐藏", document.querySelector("#trace-detail").hidden, true);
  // Switching back must not resurrect the previous run either.
  await selectCase(window.__demoCaseId);
  await waitFor("切回案件", () => (text("#case-title") || "").includes("合成演示"), 15000);
  expectMatch("切回后仍为空态", text("#lab-result") || "", /运行后将在这里显示/);
  expectMatch("切回后轨迹仍为空态", text("#agent-trace") || "", /执行后显示各步骤/);
  return {failures, notes, empty_case_id: created.id};
"""

FLOWS = ("metrics", "chat", "agent", "workbench")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", default="metrics", choices=FLOWS)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "browser-check")
    parser.add_argument("--data-dir", type=Path, help="reuse an existing isolated data directory")
    parser.add_argument("--browser", help="explicit browser executable")
    args = parser.parse_args()

    from scripts.browser_driver import BrowserSession, find_browser

    browser = find_browser(args.browser) if args.flow != "agent" else None
    if args.flow != "agent" and not browser:
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
        "LAW_REVIEW_PRICING_JSON": json.dumps(PRICING, ensure_ascii=False),
    }
    log_path = output_dir / "server.log"
    steps: list[dict[str, Any]] = []
    session = None
    process = None
    stub = None
    try:
        print(f"[1/4] 准备隔离数据目录 {data_dir}")
        _seed_demo(data_dir)
        print(f"[2/4] 启动应用服务 {base_url}")
        if args.flow in ("chat", "agent", "workbench", "all"):
            from scripts.stub_model import CHAT_MODEL, EMBEDDING_MODEL, StubModel

            stub = StubModel().__enter__()
            environment.update({
                "LAW_REVIEW_LLM_URL": stub.base_url,
                "LAW_REVIEW_LLM_MODEL": CHAT_MODEL,
                "LAW_REVIEW_EMBEDDING_URL": stub.base_url,
                "LAW_REVIEW_EMBEDDING_MODEL": EMBEDDING_MODEL,
            })
            if args.flow in ("agent", "workbench"):
                environment["LAW_REVIEW_TOOL_LOOP"] = "on"
            if args.flow == "workbench":
                environment.update({
                    "LAW_REVIEW_LLM_FALLBACK": "on",
                    "LAW_REVIEW_LLM_FALLBACK_URL": stub.base_url,
                    "LAW_REVIEW_LLM_FALLBACK_MODEL": "fallback-model",
                })
            print(f"      模型测试替身：{stub.base_url}（固定答案，不是真实模型）")
        else:
            _seed_usage(data_dir, 1)
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
            if args.flow == "metrics":
                usage = _api_json(base_url, f"/api/cases/{demo_id}/platform-metrics")["llm_usage"]
                if usage["calls"] != 3:
                    raise RuntimeError(f"unexpected seeded usage: {usage}")
            print(f"[3/4] 执行 {args.flow} 流程")
            if args.flow == "agent":
                assert stub is not None
                steps.extend(_agent_flow(base_url, demo_id, stub.prompts))
            else:
                with BrowserSession(base_url + "/", browser=browser) as opened:
                    session = opened
                    session.wait_until("document.readyState === 'complete'")
                    session.evaluate(f"window.__demoCaseId = {demo_id}; window.__emptyCaseId = null;")
                    if args.flow in ("metrics", "all"):
                        steps.append(_run_step(session, "00-open", _script(BODY_OPEN_METRICS), output_dir, False))
                        steps.append(_run_step(session, "01-partial", _script(BODY_PARTIAL), output_dir, True, "#usage-panel"))
                        steps.append(_run_step(session, "02-empty", _script(BODY_EMPTY_CASE), output_dir, True, "#usage-panel"))
                        steps.append(_run_step(session, "03-rapid-switch", _script(BODY_RAPID_SWITCH), output_dir, True, "#usage-panel"))
                        steps.append(_run_step(session, "04-failure", _script(BODY_FAILURE), output_dir, True, "#usage-panel"))
                        steps.append(_run_step(session, "05-recovery", _script(BODY_RECOVERY), output_dir, True, "#usage-panel"))
                    if args.flow in ("workbench", "all"):
                        steps.append(_run_step(session, "wb-01-run", _script(BODY_WB_RUN), output_dir, True, ".lab-layout"))
                        steps.append(_run_step(session, "wb-02-trace", _script(BODY_WB_TRACE), output_dir, True, "#agent-trace"))
                        steps.append(_run_step(session, "wb-03-fallback", _script(BODY_WB_FALLBACK), output_dir, True, ".lab-layout"))
                        steps.append(_run_step(session, "wb-04-switch", _script(BODY_WB_SWITCH), output_dir, True, ".lab-layout"))
                    if args.flow in ("chat", "all"):
                        baseline = [int(item["id"]) for item in _api_json(base_url, f"/api/cases/{demo_id}/conversations")]
                        session.evaluate(f"window.__conversationBaseline = {json.dumps(baseline)};")
                        steps.append(_run_step(session, "chat-01-answer", _script(BODY_CHAT_OPEN), output_dir, True, ".chat-panel"))
                        steps.append(_run_step(session, "chat-02-retract", _script(BODY_CHAT_RETRACT), output_dir, True, ".chat-panel"))
                        steps.append(_run_step(session, "chat-03-replay", _script(BODY_CHAT_REPLAY), output_dir, False))
                        steps.append(_run_step(session, "chat-04-state", _script(BODY_CHAT_DB_STATE), output_dir, False))
    finally:
        if session is not None:
            session.close()
        if stub is not None:
            stub.__exit__(None, None, None)
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
        "model": "stub" if args.flow in ("chat", "agent", "workbench", "all") else "not used",
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
