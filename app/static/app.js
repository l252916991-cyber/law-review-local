// ========================================
// 常量定义
// ========================================
const CONSTANTS = {
  MIN_TITLE_LENGTH: 2,
  TOAST_DURATION: 3600,
  MIN_FACT_LENGTH: 1,
};

// ========================================
// 全局状态
// ========================================
const state = {
  cases: [],
  caseId: null,
  case: null,
  documents: [],
  evidence: [],
  relations: [],
  conversations: [],
  conversationId: null,
  directoryFilter: "全部",
  directoryQuery: "",
  recoverableAgentRunId: null,
};

// ========================================
// 工具函数
// ========================================
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const toggle = $("#theme-toggle");
  if (!toggle) return;
  const dark = theme === "dark";
  toggle.querySelector("span").textContent = dark ? "☀" : "☾";
  toggle.setAttribute("aria-label", dark ? "切换到白天主题" : "切换到黑夜主题");
  toggle.title = dark ? "切换到白天主题" : "切换到黑夜主题";
}

function initializeTheme() {
  const saved = localStorage.getItem("lexvault-theme");
  const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  applyTheme(saved === "dark" || saved === "light" ? saved : preferred);
}

/**
 * API 请求封装
 * @param {string} path - API 路径
 * @param {object} options - fetch 选项
 * @returns {Promise} JSON 响应或 Response 对象
 */
async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 401 && (!path.startsWith("/api/auth/") || options.method === "DELETE")) expireSession();
  if (!response.ok) {
    let message = `请求失败(${response.status})`;
    let detail = null;
    try {
      const body = await response.json();
      detail = body.detail;
      message = typeof detail === "object" ? (detail.message || message) : (detail || message);
    } catch (e) {
      console.warn('Failed to parse error response:', e);
    }
    const error = new Error(message);
    error.status = response.status;
    if (typeof detail === "object" && detail) {
      error.runId = detail.run_id;
      error.resumable = Boolean(detail.resumable);
    }
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response;
}

/**
 * HTML 转义，防止 XSS 攻击
 * @param {string} value - 需要转义的字符串
 * @returns {string} 转义后的安全字符串
 */
function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

/**
 * Markdown 渲染(支持有限的语法)
 * @param {string} value - Markdown 文本
 * @returns {string} HTML 字符串
 */
function markdown(value = "") {
  const safe = escapeHtml(value);
  const lines = safe.split("\n");
  let inList = false;
  const output = [];
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\[\s*资料\s*(\d+)\s*\]/g, '<span class="route-badge">资料$1</span>');
    if (line.startsWith("- ")) {
      if (!inList) { output.push("<ul>"); inList = true; }
      output.push(`<li>${line.slice(2)}</li>`);
    } else {
      if (inList) { output.push("</ul>"); inList = false; }
      if (line.startsWith("## ")) output.push(`<h4>${line.slice(3)}</h4>`);
      else if (line.startsWith("&gt; ")) output.push(`<p class="model-note">${line.slice(5)}</p>`);
      else if (line.trim()) output.push(`<p>${line}</p>`);
    }
  }
  if (inList) output.push("</ul>");
  return output.join("");
}

/**
 * 显示 Toast 通知
 * @param {string} message - 消息内容
 * @param {string} tone - 消息类型：success | error | info
 */
function toast(message, tone = "success") {
  const node = document.createElement("div");
  node.className = `toast ${tone}`;
  node.textContent = message;
  $("#toast-stack").append(node);
  setTimeout(() => node.remove(), CONSTANTS.TOAST_DURATION);
}

/**
 * 格式化日期
 * @param {string} value - ISO 日期字符串
 * @returns {string} 格式化后的日期
 */
function formatDate(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/**
 * 根据状态返回样式类名
 * @param {string} status - 状态字符串
 * @returns {string} CSS 类名
 */
function statusClass(status = "") {
  return /确认|校准|完成/.test(status) ? "confirmed" : /待/.test(status) ? "pending" : "";
}

// ========================================
// 应用初始化
// ========================================

/**
 * 应用启动入口
 */
function expireSession() {
  if (document.body.classList.contains("auth-pending")) return;
  document.body.classList.add("auth-pending");
  sessionStorage.removeItem("lexvault-active-job");
  const view = $(".nav-item.active")?.dataset.view || "overview";
  if (state.caseId) sessionStorage.setItem("lexvault-return", JSON.stringify({ caseId: state.caseId, view }));
  location.replace("/?auth_error=expired");
}

const authErrors = {
  expired: "登录状态已过期，请重新登录。",
  state: "登录会话已过期或无效，请重新发起登录。",
  unavailable: "律所身份服务暂不可用，请稍后重试。",
  denied: "你的账号尚未获得此工作空间的访问权限，请联系律所管理员。",
  failed: "组织登录验证失败，请重新登录。",
};

async function checkIdentity() {
  $("#auth-error").textContent = "";
  $("#auth-retry").hidden = true;
  $("#auth-status").textContent = "正在检查登录状态…";
  try {
    const identity = await api("/api/auth/me");
    $("#auth-support").textContent = identity.support_contact || "请联系律所管理员";
    $("#auth-status").textContent = "";
    if (!identity.authenticated) {
      $("#auth-methods").hidden = false;
      $("#auth-sso").hidden = !identity.oidc_enabled;
      $("#auth-token-details").open = !identity.oidc_enabled;
      $("#auth-token-details summary").hidden = !identity.oidc_enabled;
      const url = new URL(location.href);
      const reason = url.searchParams.get("auth_error");
      $("#auth-error").textContent = reason ? (authErrors[reason] || authErrors.failed) : "";
      if (reason) { url.searchParams.delete("auth_error"); history.replaceState(null, "", url.pathname + url.search + url.hash); }
      $(identity.oidc_enabled ? "#auth-sso" : "#auth-token").focus();
      return null;
    }
    return identity;
  } catch (error) {
    $("#auth-status").textContent = "暂时无法进入工作空间";
    $("#auth-error").textContent = error.status ? error.message : "无法连接工作空间，请检查网络后重试。";
    $("#auth-methods").hidden = true;
    $("#auth-retry").hidden = false;
    return null;
  }
}

async function bootstrap() {
  bindEvents();
  const identity = await checkIdentity();
  if (!identity) return;
  document.body.classList.remove("auth-pending");
  try {
    if (identity.mode === "token") {
      $("#workspace-access").textContent = "律所授权空间";
      $("#user-name").value = identity.name;
      $("#user-name").readOnly = true;
      $("#new-case-btn").hidden = !identity.permissions.includes("manage") && !identity.admin;
      $("#sign-out").hidden = false;
    }
    const health = await api("/api/health");
    $("#model-dot").classList.toggle("online", health.local_llm);
    $("#model-state").textContent = health.local_llm ? "本地模型在线" : "规则检索模式";
    $("#model-name").textContent = health.local_llm ? health.model : "仍可使用检索与溯源";
    await loadCases();
    let returnTo = null;
    try { returnTo = JSON.parse(sessionStorage.getItem("lexvault-return")); } catch { /* Ignore invalid navigation state. */ }
    sessionStorage.removeItem("lexvault-return");
    const restoredCase = state.cases.find((item) => item.id === returnTo?.caseId);
    if (state.cases.length) {
      await selectCase(restoredCase ? restoredCase.id : state.cases[0].id);
      if (restoredCase && $$(".nav-item").some((item) => item.dataset.view === returnTo.view)) showView(returnTo.view);
    } else clearCaseWorkspace();
    const activeJob = sessionStorage.getItem("lexvault-active-job");
    if (activeJob) {
      const job = await api(`/api/agent-jobs/${activeJob}`);
      if (job.case_id !== state.caseId) await selectCase(job.case_id);
      showView("lab");
      setLabBusy(true);
      try { renderAgentResult(await pollReviewJob(activeJob)); }
      catch (error) { renderRecoverableFailure(error); }
      finally { setLabBusy(false); }
    }
  } catch (error) {
    if ([401, 404].includes(error.status)) sessionStorage.removeItem("lexvault-active-job");
    toast(error.message, "error");
  }
}

// ========================================
// 案件管理
// ========================================

/**
 * 加载所有案件列表
 */
async function loadCases() {
  state.cases = await api("/api/cases");
  renderCaseList();
}

/**
 * 渲染案件列表
 */
function openLifecycleDialog(options) {
  return new Promise((resolve) => {
    const dialog = $("#case-lifecycle-dialog");
    $("#lifecycle-kicker").textContent = options.kicker || "案卷生命周期";
    $("#lifecycle-title").textContent = options.title;
    $("#lifecycle-message").textContent = options.message;
    $("#lifecycle-impact").textContent = options.impact || "操作会写入审计记录。";
    const confirm = $("#lifecycle-confirm"); confirm.textContent = options.action || "确认"; confirm.classList.toggle("danger-action", Boolean(options.danger));
    const finish = (value) => { dialog.close(); resolve(value); };
    $("#lifecycle-cancel").onclick = () => finish(false); $("#lifecycle-cancel-action").onclick = () => finish(false); confirm.onclick = () => finish(true);
    dialog.addEventListener("cancel", () => resolve(false), { once: true }); dialog.showModal();
  });
}

function clearCaseWorkspace() {
  Object.assign(state, {caseId: null, case: null, documents: [], evidence: [], relations: [], conversations: [], conversationId: null, recoverableAgentRunId: null});
  $("#case-title").textContent = "暂无工作区案件";
  $("#case-type").textContent = "案件空间";
  $$(".view").forEach((view) => view.classList.remove("active"));
  $("#no-case-state").hidden = false;
}

async function refreshAfterCaseRemoval(id) {
  await loadCases();
  if (state.caseId === id) {
    if (state.cases[0]) await selectCase(state.cases[0].id);
    else clearCaseWorkspace();
  }
}

async function openCaseStorage(status) {
  const dialog = $("#case-storage-dialog");
  $("#case-storage-title").textContent = status === "archived" ? "已归档案卷" : "回收站";
  $("#case-storage-description").textContent = status === "archived" ? "长期保留，可随时恢复到案件空间。" : "删除的案卷保留 30 天，可在清理前恢复。";
  $("#case-storage-list").textContent = "正在加载…";
  if (!dialog.open) dialog.showModal();
  try {
    const items = await api(`/api/cases/lifecycle/${status}`);
    $("#case-storage-list").innerHTML = items.map((item) => `<div class="storage-row"><div><strong>${escapeHtml(item.title)}</strong><small>${status === "archived" ? "长期保留" : `保留至 ${escapeHtml((item.purge_after || "").slice(0,10))}`}</small></div><button type="button" class="secondary-button" data-restore-case="${item.id}">恢复案卷</button></div>`).join("") || '<div class="empty-state">暂无案卷</div>';
    $$("[data-restore-case]", dialog).forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const id = Number(button.dataset.restoreCase);
        await api(`/api/cases/${id}/restore`, {method: "POST"});
        await loadCases(); await selectCase(id); showView("overview");
        await openCaseStorage(status); toast("案卷已恢复到案件空间");
      } catch (error) { toast(error.message, "error"); button.disabled = false; }
    }));
  } catch (error) { $("#case-storage-list").textContent = error.message; }
}

function renderCaseList() {
  $("#case-list").innerHTML = state.cases.map((item) => `
    <div class="case-row">
      <button class="case-item ${item.id === state.caseId ? "active" : ""}" data-case-id="${item.id}">
        <strong>${escapeHtml(item.title)}</strong><small>${item.document_count} 份卷宗 · ${item.evidence_count} 条证据</small>
      </button>
      <details class="case-menu"><summary aria-label="案卷操作">···</summary><div><button data-archive-case="${item.id}">归档案卷</button><button class="case-delete" data-trash-case="${item.id}">移入回收站</button></div></details>
    </div>`).join("") || '<div class="empty-state">暂无案件</div>';
  $$(".case-menu").forEach((menu) => menu.addEventListener("toggle", () => {
    if (!menu.open) return;
    const rect = menu.querySelector("summary").getBoundingClientRect();
    const panel = menu.querySelector("div");
    panel.style.left = `${Math.max(8, rect.right - 144)}px`;
    panel.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - 100)}px`;
    $$(".case-menu").forEach((other) => { if (other !== menu) other.open = false; });
  }));
  $$(".case-item").forEach((button) => button.addEventListener("click", () => selectCase(Number(button.dataset.caseId))));
  $$('[data-archive-case]').forEach((button) => button.addEventListener("click", async (event) => {
    event.stopPropagation();
    const id = Number(button.dataset.archiveCase);
    if (!(await openLifecycleDialog({ title: "归档案卷？", kicker: "长期保留", message: "归档后案卷会从日常工作区移出，但会长期保留并可随时恢复。", action: "归档案卷" }))) return;
    try { await api(`/api/cases/${id}/archive`, { method: "POST" }); await refreshAfterCaseRemoval(id); toast("案卷已归档"); } catch (error) { toast(error.message, "error"); }
  }));
  $$('[data-trash-case]').forEach((button) => button.addEventListener("click", async (event) => {
    event.stopPropagation();
    const id = Number(button.dataset.trashCase);
    const item = state.cases.find((candidate) => candidate.id === id);
    if (!item) return;
    if (!(await openLifecycleDialog({ title: "移入回收站？", kicker: "可恢复删除", message: `案卷「${item.title}」将移入回收站，保留 30 天。`, impact: `将影响 ${item.document_count} 份卷宗、${item.evidence_count} 条证据。`, action: "移入回收站", danger: true }))) return;
    try { await api(`/api/cases/${id}/trash`, { method: "POST" }); await refreshAfterCaseRemoval(id); toast("案卷已移入回收站，保留 30 天"); } catch (error) { toast(error.message, "error"); }
  }));
}

/**
 * 选择并加载案件详情
 * @param {number} caseId - 案件 ID
 */
async function selectCase(caseId) {
  $("#no-case-state").hidden = true;
  state.caseId = caseId;
  if (!$(".view.active")) showView("overview");
  state.conversationId = null;

  // 显示加载状态
  showLoadingState();

  try {
    const [caseData, documents, evidenceData, conversations, activity] = await Promise.all([
      api(`/api/cases/${caseId}`),
      api(`/api/cases/${caseId}/documents`),
      api(`/api/cases/${caseId}/evidence`),
      api(`/api/cases/${caseId}/conversations`),
      api(`/api/cases/${caseId}/audit`),
    ]);
    state.case = caseData;
    state.documents = documents;
    state.evidence = evidenceData.evidence;
    state.relations = evidenceData.relations;
    state.conversations = conversations;
    $("#case-title").textContent = caseData.title;
    $("#case-type").textContent = `${caseData.case_type} · ${caseData.status}`;
    $("#case-description").textContent = caseData.description || "";
    $("#case-description").hidden = !caseData.description;
    renderCaseList();
    renderOverview(activity);
    renderDirectory();
    renderEvidence();
    renderConversations();
    resetChat();
    await loadLabMetrics();
  } catch (error) {
    toast(`加载案件失败: ${error.message}`, "error");
  } finally {
    hideLoadingState();
  }
}

/**
 * 显示加载状态
 */
function showLoadingState() {
  $("#evidence-grid").innerHTML = '<div class="loading-state">⏳ 加载中...</div>';
  $("#directory-body").innerHTML = '<tr><td colspan="7"><div class="loading-state">⏳ 加载中...</div></td></tr>';
}

/**
 * 隐藏加载状态
 */
function hideLoadingState() {
  // 加载状态会被实际内容替换，无需手动清除
}

async function loadLabMetrics() {
  if (!state.caseId) return;
  try {
    const [metrics, benchmark] = await Promise.all([
      api(`/api/cases/${state.caseId}/platform-metrics`),
      api("/api/benchmarks/lawbench?limit=0"),
    ]);
    const runs = metrics.agent_runs || {};
    const vectors = metrics.vector_index || {};
    const evaluation = metrics.evaluation;
    $("#lab-run-count").textContent = runs.total || 0;
    $("#lab-run-success").textContent = `${runs.completed || 0} 成功 · ${runs.failed || 0} 失败 · 均值 ${Math.round(runs.avg_ms || 0)}ms`;
    $("#lab-vector-pages").textContent = vectors.pages || 0;
    $("#lab-vector-meta").textContent = vectors.pages ? `${vectors.pages} 页可用于检索` : "等待构建";
    $("#lab-memory-count").textContent = metrics.memory_count || 0;
    $("#lab-recall").textContent = evaluation?.recall_at_k != null ? `${Math.round(evaluation.recall_at_k * 100)}%` : "—";
    $("#lab-mrr").textContent = evaluation ? `排序得分 ${evaluation.mrr == null ? "—" : evaluation.mrr.toFixed(2)} · 原文片段提供率 ${Math.round((evaluation.quote_presence_rate || 0) * 100)}%` : "等待质量检查";
    $("#benchmark-chip").textContent = `LawBench ${benchmark.total_questions.toLocaleString("zh-CN")} 题`;
    const resumable = (metrics.recent_runs || []).filter((run) => run.resumable);
    const recoveryTarget = $("#lab-resumable-runs");
    recoveryTarget.hidden = resumable.length === 0;
    recoveryTarget.innerHTML = resumable.map((run) => `<button class="secondary-button resume-button" data-resume-run="${run.id}">继续执行 任务 #${run.id} · ${escapeHtml(run.question.slice(0, 24))}</button>`).join("");
    $$('[data-resume-run]', recoveryTarget).forEach((button) => button.addEventListener("click", resumeAgentRun));
  } catch (error) { console.warn("Agent metrics unavailable", error); }
}

function renderAgentResult(result) {
  state.recoverableAgentRunId = null;
  $("#runtime-comparison").hidden = true;
  const citationHtml = result.citations?.length ? `<div class="citations">${result.citations.map((item) => `<div class="citation" data-preview="${item.document_id}" data-page="${item.page}"><span class="citation-index">${item.index}</span><div><strong>${escapeHtml(item.document_name)}</strong><small>${escapeHtml(item.quote)}</small><em>${escapeHtml(item.retrieval_explain || "")}</em></div><span class="citation-page">第 ${item.page} 页 →</span></div>`).join("")}</div>` : "";
  const modelBadge = result.llm_model ? `<span class="route-badge secondary">${escapeHtml(result.llm_model)}</span>` : "";
  const fallbackBadge = result.fallback_reason ? '<span class="route-badge secondary">规则降级</span>' : "";
  const checkpointBadge = result.runtime === "langgraph" ? `<span class="route-badge secondary">恢复记录 ${Number(result.checkpoint_size_bytes || 0).toLocaleString("zh-CN")} B · 恢复 ${result.resume_count || 0} 次</span>` : "";
  $("#lab-result").innerHTML = `<div class="lab-result-meta"><span class="route-badge">${escapeHtml(result.agent_type)}</span><span class="route-badge secondary">${escapeHtml(result.route)}</span>${modelBadge}${fallbackBadge}${checkpointBadge}<span class="route-badge secondary">${result.total_ms}ms</span></div><div class="answer-text">${markdown(result.answer)}</div>${citationHtml}`;
  $$('[data-preview]', $("#lab-result")).forEach((node) => node.addEventListener("click", () => openPage(Number(node.dataset.preview), Number(node.dataset.page))));
  $("#trace-total").textContent = `任务 #${result.run_id} · ${result.total_ms}ms`;
  $("#agent-trace").innerHTML = result.steps.map((step, index) => `<div class="trace-step"><span>${String(index + 1).padStart(2, "0")}</span><div><strong>${escapeHtml(step.role)}</strong><small>${escapeHtml(step.summary || step.node)}</small></div><b>${step.latency_ms}ms</b><i>${escapeHtml(step.status)}</i></div>`).join("");
}

function comparisonCard(label, result) {
  const nodes = (result.steps || []).map((step) => `${step.role} ${step.latency_ms}ms`).join(" · ");
  const citations = (result.citations || []).map((item) => `<button class="comparison-citation" data-preview="${item.document_id}" data-page="${item.page}">[资料${item.index}] ${escapeHtml(item.document_name)} · 第 ${item.page} 页 →</button>`).join("");
  return `<article class="comparison-card"><header><strong>${escapeHtml(label)}</strong><span>任务 #${result.run_id} · ${result.total_ms}ms</span></header><div class="answer-text">${markdown(result.answer)}</div><div class="comparison-citations">${citations || "暂无引用"}</div><div class="comparison-nodes">${escapeHtml(nodes)}</div></article>`;
}

function renderComparisonResult(result) {
  const target = $("#runtime-comparison");
  const checks = [
    ["分析结构", result.comparison.structurally_equivalent],
    ["全部检查项", result.comparison.equivalent],
    ["检索路径", result.comparison.route_match],
    ["引用", result.comparison.citation_match],
    ["分析步骤", result.comparison.node_match],
    ["专家输出", result.comparison.specialist_output_match],
    ["答复结构", result.comparison.answer_contract_match],
  ];
  target.innerHTML = `<div class="comparison-summary">${checks.map(([label, pass]) => `<span class="dataset-chip ${pass ? "pass" : "fail"}">${pass ? "✓" : "×"} ${label}</span>`).join("")}<span class="dataset-chip">可恢复分析耗时差 ${result.comparison.latency_delta_ms >= 0 ? "+" : ""}${result.comparison.latency_delta_ms}ms</span></div><div class="comparison-grid">${comparisonCard("标准分析", result.native)}${comparisonCard("可恢复分析", result.langgraph)}</div>`;
  const overhead = result.comparison.langgraph_overhead_percent;
  $(".comparison-summary", target).insertAdjacentHTML("beforeend", `<span class="dataset-chip">相对耗时 ${overhead === null ? "—" : `${overhead > 0 ? "+" : ""}${overhead}%`}</span><span class="dataset-chip">恢复记录 ${Number(result.comparison.checkpoint_size_bytes || 0).toLocaleString("zh-CN")} B</span>`);
  $$('[data-preview]', target).forEach((node) => node.addEventListener("click", () => openPage(Number(node.dataset.preview), Number(node.dataset.page))));
  target.hidden = false;
  $("#lab-result").innerHTML = '<div class="empty-state">两种分析方式已使用相同问题完成。请对比答复、引用和各项检查结果。</div>';
  $("#trace-total").textContent = `标准分析 #${result.native.run_id} ↔ 可恢复分析 #${result.langgraph.run_id}`;
  $("#agent-trace").innerHTML = [...result.native.steps, ...result.langgraph.steps].map((step, index) => `<div class="trace-step"><span>${String(index + 1).padStart(2, "0")}</span><div><strong>${escapeHtml(step.role)}</strong><small>${escapeHtml(step.summary || step.node)}</small></div><b>${step.latency_ms}ms</b><i>${escapeHtml(step.status)}</i></div>`).join("");
}

function renderRecoverableFailure(error) {
  $("#runtime-comparison").hidden = true;
  state.recoverableAgentRunId = error.resumable ? error.runId : null;
  const resume = error.resumable && error.runId ? `<button class="secondary-button resume-button" id="resume-agent-btn">继续执行 任务 #${error.runId}</button>` : "";
  $("#lab-result").innerHTML = `<div class="empty-state">运行失败：${escapeHtml(error.message)}${resume}</div>`;
  if (resume) $("#resume-agent-btn").addEventListener("click", resumeAgentRun);
}

async function resumeAgentRun(event) {
  const button = event?.currentTarget || $("#resume-agent-btn");
  const runId = Number(button?.dataset.resumeRun) || state.recoverableAgentRunId;
  if (!runId) return;
  state.recoverableAgentRunId = runId;
  button.disabled = true; button.textContent = "从保存的进度恢复中…";
  try {
    const result = await api(`/api/agent-runs/${runId}/resume`, { method: "POST" });
    renderAgentResult(result);
    await loadLabMetrics();
    toast(`任务 #${runId} 已从保存的进度恢复`, "success");
  } catch (error) {
    renderRecoverableFailure(error);
    toast(error.message, "error");
    await loadLabMetrics();
  }
}

async function runAgentLab() {
  const question = $("#lab-question").value.trim();
  if (!question || !state.caseId) return;
  const button = $("#run-agent-btn");
  const mode = $("#agent-runtime").value;
  $("#compare-agent-btn").disabled = true;
  $("#agent-runtime").disabled = true;
  button.disabled = true; button.textContent = `${mode === "langgraph" ? "可恢复分析" : "标准分析"} 执行中…`;
  setLabBusy(true);
  $("#lab-result").innerHTML = '<div class="empty-state">正在安排分析步骤，请稍候…</div>';
  $("#runtime-comparison").hidden = true;
  try {
    const submitted = await api(`/api/cases/${state.caseId}/agent-jobs`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, user_name: $("#user-name").value || "本机律师", mode, use_llm: $("#lab-llm-toggle").checked, use_remote_embeddings: true }),
    });
    sessionStorage.setItem("lexvault-active-job", submitted.job_id);
    const result = await pollReviewJob(submitted.job_id);
    renderAgentResult(result);
    await loadLabMetrics();
    toast(`任务 #${result.run_id} 完成：${result.steps.length} 个步骤，${result.total_ms}ms`);
  } catch (error) {
    renderRecoverableFailure(error);
    toast(error.message, "error");
  } finally {
    setLabBusy(false);
    button.disabled = false;
    $("#compare-agent-btn").disabled = false;
    $("#agent-runtime").disabled = false;
    button.innerHTML = `运行${mode === "langgraph" ? "可恢复分析" : "标准分析"} <b>→</b>`;
  }
}

async function pollReviewJob(jobId) {
  for (;;) {
    const job = await api(`/api/agent-jobs/${jobId}`);
    $("#trace-total").textContent = `后台任务 ${job.status}${job.run_id ? ` · 任务 #${job.run_id}` : ""}`;
    $("#agent-trace").innerHTML = (job.steps || []).map((step, index) => `<div class="trace-step"><span>${index + 1}</span><div><strong>${escapeHtml(step.agent_role || step.node_name)}</strong><small>${escapeHtml(step.output?.summary || step.node_name)}</small></div><b>${Number(step.latency_ms || 0)}ms</b><i>${escapeHtml(step.status)}</i></div>`).join("");
    if (job.status === "completed") {
      sessionStorage.removeItem("lexvault-active-job");
      return job.result;
    }
    if (["failed", "interrupted"].includes(job.status)) {
      sessionStorage.removeItem("lexvault-active-job");
      const error = new Error(job.error?.message || "任务已中断，请检查运行轨迹");
      error.runId = job.error?.run_id || job.run_id;
      error.resumable = Boolean(job.resumable || job.error?.resumable);
      throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

function setLabBusy(busy) {
  ["#run-agent-btn", "#compare-agent-btn", "#agent-runtime"].forEach((id) => { $(id).disabled = busy; });
  $$(".case-item").forEach((button) => { button.disabled = busy; });
}

async function compareAgentRuntimes() {
  const question = $("#lab-question").value.trim();
  if (!question || !state.caseId) return;
  const button = $("#compare-agent-btn");
  $("#run-agent-btn").disabled = true;
  $("#agent-runtime").disabled = true;
  button.disabled = true; button.textContent = "依次运行两版中…";
  $("#runtime-comparison").hidden = true;
  $("#lab-result").innerHTML = '<div class="empty-state">依次运行两种分析方式；对比结果不会写入案件记忆。</div>';
  try {
    const result = await api(`/api/cases/${state.caseId}/agent-compare`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, user_name: $("#user-name").value || "本机律师", mode: "multi_agent", use_llm: $("#lab-llm-toggle").checked, use_remote_embeddings: true }),
    });
    renderComparisonResult(result);
    await loadLabMetrics();
    toast(result.comparison.equivalent ? "两种分析方式的检查项一致" : "两种分析方式的检查项存在差异", result.comparison.equivalent ? "success" : "error");
  } catch (error) {
    renderRecoverableFailure(error);
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    $("#run-agent-btn").disabled = false;
    $("#agent-runtime").disabled = false;
    button.textContent = "分析方式对比";
  }
}

async function buildHybridIndex() {
  const button = $("#build-index-btn");
  button.disabled = true; button.textContent = "索引构建中…";
  try {
    const result = await api(`/api/cases/${state.caseId}/vector-index`, { method: "POST" });
    toast(`检索索引已更新：${result.indexed.pages} 页，新增 ${result.indexed.embedded} 页`);
    await loadLabMetrics();
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = "更新检索索引"; }
}

async function runEvaluation() {
  const button = $("#evaluate-btn");
  button.disabled = true; button.textContent = "评测运行中…";
  try {
    const report = await api(`/api/cases/${state.caseId}/evaluate-rag`, { method: "POST" });
    const metric = (value) => value == null ? "—" : Number(value).toFixed(2);
    $("#evaluation-result").innerHTML = `<div class="eval-score"><strong>${report.recall_at_k == null ? "—" : `${Math.round(report.recall_at_k * 100)}%`}</strong><span>前 ${report.k} 项召回率</span></div><div class="eval-grid"><div><b>${metric(report.mrr)}</b><small title="标准答案首次出现位置的倒数均值">排序得分</small></div><div><b>${Math.round((report.quote_presence_rate || 0) * 100)}%</b><small>原文片段提供率</small></div><div><b>${report.average_latency_ms}ms</b><small>平均延迟</small></div><div><b>${report.queries}</b><small>标准答案题数</small></div></div><p>片段提供率不代表模型论断受到原文支持；需律师人工复核。</p><div class="eval-cases">${report.cases.map((item) => `<div><span class="${item.recall_at_k >= .5 ? "pass" : "fail"}">${item.recall_at_k == null ? "无答案题" : item.recall_at_k >= .5 ? "达标" : "未达标"}</span><p>${escapeHtml(item.query)}</p><b>召回率 ${metric(item.recall_at_k)} · 排序得分 ${metric(item.mrr)}</b></div>`).join("")}</div>`;
    await loadLabMetrics(); toast("检索质量检查完成");
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = "检查检索质量"; }
}

function renderOverview(activity = []) {
  const m = state.case.metrics;
  $("#metric-docs").textContent = m.documents;
  $("#metric-pages").textContent = m.pages;
  $("#metric-evidence").textContent = m.evidence;
  $("#metric-confirmed").textContent = m.confirmed;
  $("#activity-list").innerHTML = activity.slice(0, 6).map((item) => `
    <div class="activity"><div class="activity-mark">${activityIcon(item.action)}</div><div><strong>${escapeHtml(item.action)}</strong><small>${escapeHtml(item.detail)}</small></div><time>${formatDate(item.created_at)}</time></div>
  `).join("") || '<div class="empty-state">尚无操作记录</div>';
}

function activityIcon(action) {
  if (action.includes("上传")) return "⇧";
  if (action.includes("问答")) return "✦";
  if (action.includes("目录")) return "▤";
  if (action.includes("导出")) return "⇩";
  return "✓";
}

function renderDirectory() {
  const query = state.directoryQuery.toLowerCase();
  const rows = state.documents.filter((doc) => {
    const matchesFilter = state.directoryFilter === "全部" || doc.doc_type === state.directoryFilter;
    const haystack = `${doc.name} ${doc.doc_type} ${doc.people} ${doc.date_range} ${doc.summary}`.toLowerCase();
    return matchesFilter && (!query || haystack.includes(query));
  });
  $("#directory-count").textContent = `${rows.length} 条记录`;
  $("#directory-body").innerHTML = rows.map((doc) => `
    <tr>
      <td><div class="doc-cell"><span class="doc-icon">${doc.name.toLowerCase().endsWith(".pdf") ? "PDF" : "文"}</span><div><strong>${escapeHtml(doc.name)}</strong><small>${escapeHtml(doc.summary)}</small></div></div></td>
      <td><span class="type-badge">${escapeHtml(doc.doc_type)}</span></td>
      <td>${escapeHtml(doc.people || "—")}</td><td>${escapeHtml(doc.date_range || "—")}</td><td>${doc.pages} 页</td>
      <td><span class="status-badge ${statusClass(doc.status)}">${escapeHtml(doc.status)}</span></td>
      <td><div class="row-actions"><button data-preview="${doc.id}" data-page="1">原页</button><button data-edit="${doc.id}">校准</button></div></td>
    </tr>`).join("") || '<tr><td colspan="7"><div class="empty-state">没有匹配的卷宗记录</div></td></tr>';
  $$('[data-preview]').forEach((button) => button.addEventListener("click", () => openPage(Number(button.dataset.preview), Number(button.dataset.page))));
  $$('[data-edit]').forEach((button) => button.addEventListener("click", () => openDirectoryEdit(Number(button.dataset.edit))));
}

function openDirectoryEdit(documentId) {
  const doc = state.documents.find((item) => item.id === documentId);
  if (!doc) return;
  $("#edit-document-id").value = doc.id;
  $("#edit-document-name").textContent = doc.name;
  $("#edit-doc-type").value = doc.doc_type;
  $("#edit-people").value = doc.people;
  $("#edit-date-range").value = doc.date_range;
  $("#edit-status").value = [...$("#edit-status").options].some((x) => x.value === doc.status) ? doc.status : "已索引";
  $("#edit-summary").value = doc.summary;
  $("#directory-dialog").showModal();
}

async function saveDirectory() {
  const id = Number($("#edit-document-id").value);
  const body = {
    doc_type: $("#edit-doc-type").value,
    people: $("#edit-people").value,
    date_range: $("#edit-date-range").value,
    status: $("#edit-status").value,
    summary: $("#edit-summary").value,
  };
  try {
    await api(`/api/documents/${id}/directory`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#directory-dialog").close();
    toast("目录已保存，检索内容已更新");
    await selectCase(state.caseId);
  } catch (error) { toast(error.message, "error"); }
}

async function openPage(documentId, pageNo) {
  try {
    const page = await api(`/api/documents/${documentId}/pages/${pageNo}`);
    $("#page-dialog-title").textContent = page.name;
    $("#page-dialog-meta").textContent = `${page.doc_type} · 第 ${page.page_no} 页 · 文档 ID ${documentId}`;
    $("#page-dialog-text").textContent = page.text || "该页未识别到文本";
    $("#page-dialog").showModal();
  } catch (error) { toast(error.message, "error"); }
}

async function openAnnotations(evidenceId) {
  const dialog = $("#annotations-dialog");
  dialog.dataset.evidenceId = evidenceId;
  $("#annotation-form").reset();
  $("#annotations-list").textContent = "加载标注…";
  if (!dialog.open) dialog.showModal();
  try {
    const items = await api(`/api/evidence/${evidenceId}/annotations`);
    if (Number(dialog.dataset.evidenceId) !== evidenceId) return;
    $("#annotations-list").innerHTML = items.map((item) => `<article class="annotation-card"><strong>${escapeHtml(item.annotation_type)} · ${escapeHtml(item.status)}</strong><p>${escapeHtml(item.content)}</p><small>${escapeHtml(item.user_name)} · ${escapeHtml(formatDate(item.created_at))}</small><div><button class="text-button" data-edit-annotation="${item.id}">编辑</button><button class="text-button" data-delete-annotation="${item.id}">删除</button></div></article>`).join("") || '<p class="empty-state">暂无标注</p>';
    $$('[data-edit-annotation]').forEach((button) => button.addEventListener("click", () => {
      const item = items.find((row) => row.id === Number(button.dataset.editAnnotation));
      $("#annotation-id").value = item.id;
      $("#annotation-type").value = item.annotation_type;
      $("#annotation-content").value = item.content;
      $("#annotation-status").value = item.status;
      $("#annotation-content").focus();
    }));
    $$('[data-delete-annotation]').forEach((button) => button.addEventListener("click", async () => {
      if (!confirm("确定删除这条标注？此操作不可撤销。")) return;
      try {
        await api(`/api/evidence-annotations/${Number(button.dataset.deleteAnnotation)}`, { method: "DELETE" });
        await openAnnotations(evidenceId);
      } catch (error) { toast(error.message, "error"); }
    }));
  } catch (error) { $("#annotations-list").textContent = error.message; }
}

async function saveAnnotation(event) {
  event.preventDefault();
  const id = $("#annotation-id").value;
  const evidenceId = Number($("#annotations-dialog").dataset.evidenceId);
  const body = { annotation_type: $("#annotation-type").value, content: $("#annotation-content").value.trim(), status: $("#annotation-status").value };
  if (!body.content) return;
  if (!id) body.user_name = $("#user-name").value || "本机律师";
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    await api(id ? `/api/evidence-annotations/${Number(id)}` : `/api/evidence/${evidenceId}/annotations`, { method: id ? "PATCH" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    await openAnnotations(evidenceId);
    toast("标注已保存");
  } catch (error) { toast(error.message, "error"); }
  finally { if (button) button.disabled = false; }
}

function renderEvidence() {
  const confirmed = state.evidence.filter((x) => /确认/.test(x.status)).length;
  const conflicts = state.relations.filter((x) => x.relation_type.includes("矛盾")).length;
  const pending = state.evidence.filter((x) => /待/.test(x.status)).length;
  $("#evidence-summary").innerHTML = [
    ["证据事项", state.evidence.length], ["已确认", confirmed], ["矛盾关系", conflicts], ["待律师复核", pending],
  ].map(([label, value]) => `<article><span>${label}</span><strong>${value}</strong></article>`).join("");
  $("#evidence-count").textContent = `${state.evidence.length} 条`;
  $("#evidence-grid").innerHTML = state.evidence.map((item) => `
    <article class="evidence-card">
      <header>
        <span class="type-badge">${escapeHtml(item.category)}</span>
        <span class="credibility">可信度 ${escapeHtml(item.credibility)}</span>
      </header>
      <h4>${escapeHtml(item.title)}</h4>
      <p>${escapeHtml(item.fact)}</p>
      <div class="evidence-source">"${escapeHtml(item.quote)}"</div>
      <div class="evidence-actions">
        <button class="source-link" data-preview="${item.source_document_id}" data-page="${item.source_page_start}" ${!item.source_document_id ? 'disabled' : ''}>
          <span>${escapeHtml(item.source_name || "来源待补充")}</span>
          ${item.source_document_id ? `<b>第 ${item.source_page_start} 页 →</b>` : ''}
        </button>
        <button class="icon-button" data-edit-evidence="${item.id}" title="编辑">✎</button>
        <button class="icon-button" data-annotations="${item.id}" title="查看与添加标注">标注</button>
        <button class="icon-button" data-delete-evidence="${item.id}" title="删除" style="color: #dc6a58;">🗑</button>
      </div>
    </article>
  `).join("") || '<div class="empty-state">尚未形成证据事项，点击右上角"➕ 添加证据"开始创建</div>';
  const evidenceMap = new Map(state.evidence.map((item) => [item.id, item]));
  $("#relation-list").innerHTML = state.relations.map((rel) => {
    const from = evidenceMap.get(rel.from_evidence_id), to = evidenceMap.get(rel.to_evidence_id);
    return `<div class="relation-item"><div class="relation-path"><span>${escapeHtml(from?.title || "证据")}</span><span class="relation-arrow">→</span><span>${escapeHtml(to?.title || "证据")}</span></div><span class="relation-type">${escapeHtml(rel.relation_type)}</span><p>${escapeHtml(rel.note)}</p></div>`;
  }).join("") || '<div class="empty-state">暂无关联关系</div>';

  // 绑定事件 - 只有启用的按钮才绑定点击事件
  $$('#evidence-view [data-preview]:not([disabled])').forEach((button) => button.addEventListener("click", () => openPage(Number(button.dataset.preview), Number(button.dataset.page))));
  $$('[data-edit-evidence]').forEach((button) => button.addEventListener("click", () => openEvidenceEdit(Number(button.dataset.editEvidence))));
  $$('[data-annotations]').forEach((button) => button.addEventListener("click", () => openAnnotations(Number(button.dataset.annotations))));
  $$('[data-delete-evidence]').forEach((button) => button.addEventListener("click", () => confirmDeleteEvidence(Number(button.dataset.deleteEvidence))));


  // 渲染证据关系图谱
  renderEvidenceGraph();

  // 渲染证据时间线
  renderEvidenceTimeline();

  // 渲染疏漏检测仪表板
  renderGapDashboard();
}

// Evidence text is untrusted input: build tooltips as DOM text nodes instead
// of HTML strings, because the graph popup library interprets markup.
function evidenceTooltip(lines) {
  const box = document.createElement('div');
  box.style.cssText = 'max-width:320px;white-space:pre-wrap;font-size:12px;';
  lines.forEach((line, index) => {
    if (index > 0) box.appendChild(document.createElement('br'));
    box.appendChild(document.createTextNode(line == null ? '' : String(line)));
  });
  return box;
}

async function loadExportTemplates() {
  const selector = $("#export-template");
  if (!selector) return;
  try {
    const templates = await api("/api/export-templates");
    selector.innerHTML = '<option value="">默认结案包（完整归档）</option>' +
      templates.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}${t.builtin ? "" : "（自定义）"}</option>`).join("");
  } catch (error) {
    selector.innerHTML = '<option value="">默认结案包（完整归档）</option>';
    console.warn("Failed to load export templates:", error);
  }
}

function renderEvidenceGraph() {
  if (!state.evidence || state.evidence.length === 0) {
    $("#evidence-graph").innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ba5a2;">暂无证据数据</div>';
    return;
  }

  if (typeof vis === 'undefined') {
    $("#evidence-graph").innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ba5a2;">可视化库加载中...</div>';
    return;
  }

  // 构建节点
  const nodes = new vis.DataSet(
    state.evidence.map(e => ({
      id: e.id,
      label: e.title.length > 20 ? e.title.substring(0, 20) + '...' : e.title,
      title: evidenceTooltip([e.title, '', e.fact, '', `来源: ${e.source_name || '未知'} 第${e.source_page_start}页`]),
      color: getColorByStatus(e.status),
      shape: 'box',
      font: { size: 12, color: '#334842' },
      margin: 10
    }))
  );

  // 构建边
  const edges = new vis.DataSet(
    state.relations.map(r => ({
      from: r.from_evidence_id,
      to: r.to_evidence_id,
      label: r.relation_type,
      color: getColorByRelationType(r.relation_type),
      arrows: 'to',
      title: r.note ? evidenceTooltip([r.note]) : undefined,
      font: { size: 10, align: 'middle' }
    }))
  );

  const container = document.getElementById('evidence-graph');
  const data = { nodes, edges };
  const options = {
    physics: {
      enabled: true,
      solver: 'forceAtlas2Based',
      forceAtlas2Based: {
        gravitationalConstant: -50,
        centralGravity: 0.01,
        springLength: 150,
        springConstant: 0.08
      },
      stabilization: { iterations: 150 }
    },
    interaction: {
      hover: true,
      navigationButtons: true,
      keyboard: true,
      tooltipDelay: 200
    },
    nodes: {
      font: { size: 12 },
      borderWidth: 2,
      shadow: true
    },
    edges: {
      width: 2,
      smooth: { type: 'continuous' }
    }
  };

  const network = new vis.Network(container, data, options);
  window.evidenceNetwork = network;

  // 点击节点高亮证据链
  network.on('click', (params) => {
    if (params.nodes.length > 0) {
      const evidenceId = params.nodes[0];
      highlightEvidenceChain(evidenceId, network);
    }
  });

  // 双击节点跳转到原文
  network.on('doubleClick', (params) => {
    if (params.nodes.length > 0) {
      const evidence = state.evidence.find(e => e.id === params.nodes[0]);
      if (evidence && evidence.source_document_id) {
        openPage(evidence.source_document_id, evidence.source_page_start);
      }
    }
  });

  // 图谱控制按钮
  $('#graph-fit-btn').onclick = () => network.fit();
  $('#graph-layout-btn').onclick = () => network.stabilize();
}

function getColorByStatus(status) {
  const colors = {
    '已确认': { background: '#e8f5f0', border: '#0c765d' },
    '待复核': { background: '#fef5e7', border: '#f59e0b' },
    '待质证': { background: '#fee8e7', border: '#ef4444' },
    '待补证': { background: '#e8eaed', border: '#6b7280' }
  };
  return colors[status] || { background: '#f0f3f2', border: '#94a3b8' };
}

function getColorByRelationType(type) {
  const colors = {
    '相互印证': '#10b981',
    '相互矛盾': '#ef4444',
    '资金链路': '#3b82f6'
  };
  return { color: colors[type] || '#6b7280' };
}

function highlightEvidenceChain(evidenceId, network) {
  const chain = traceEvidenceChain(evidenceId);
  const chainIds = chain.map(e => e.id);

  // 高亮选中的节点和边
  network.selectNodes(chainIds);

  // 显示链路信息
  toast(`发现 ${chain.length} 个关联证据`);
}

function traceEvidenceChain(startId) {
  const visited = new Set();
  const chain = [];

  function dfs(evidenceId) {
    if (visited.has(evidenceId)) return;
    visited.add(evidenceId);

    const evidence = state.evidence.find(e => e.id === evidenceId);
    if (evidence) chain.push(evidence);

    // 正向追踪
    state.relations
      .filter(r => r.from_evidence_id === evidenceId)
      .forEach(r => dfs(r.to_evidence_id));

    // 反向追溯
    state.relations
      .filter(r => r.to_evidence_id === evidenceId)
      .forEach(r => dfs(r.from_evidence_id));
  }

  dfs(startId);
  return chain;
}

async function runAnalysis() {
  const button = $("#analyze-btn");
  button.disabled = true; button.textContent = "分析卷宗中…";
  try {
    const result = await api(`/api/cases/${state.caseId}/analyze`, { method: "POST" });
    toast(`扫描 ${result.scanned_pages} 页，新增 ${result.created} 条待复核事项`);
    await selectCase(state.caseId);
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = "分析证据"; }
}

function renderConversations() {
  $("#conversation-list").innerHTML = state.conversations.map((item) => `
    <button class="conversation-item ${item.id === state.conversationId ? "active" : ""}" data-conversation="${item.id}"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.user_name)} · ${item.message_count} 条消息</small></button>
  `).join("") || '<div class="empty-state">暂无会话</div>';
  $$("[data-conversation]").forEach((button) => button.addEventListener("click", () => loadConversation(Number(button.dataset.conversation))));
}

function resetChat() {
  state.conversationId = null;
  renderConversations();
  $("#chat-messages").innerHTML = '<div class="welcome-message"><h2>案件问答</h2><p>输入需要核查的问题。请结合引用原文复核答复。</p></div>';
}

async function loadConversation(id) {
  state.conversationId = id;
  renderConversations();
  try {
    const messages = await api(`/api/conversations/${id}/messages`);
    const container = $("#chat-messages"); container.innerHTML = "";
    for (const message of messages) appendMessage(message.role, message.content, message.route, message.citations || []);
    container.scrollTop = container.scrollHeight;
  } catch (error) { toast(error.message, "error"); }
}

function appendMessage(role, content, route = "", citations = [], semantic = [], llmUsed = false) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  if (role === "user") node.innerHTML = `<div class="bubble">${escapeHtml(content)}</div>`;
  else {
    const citationHtml = citations.length ? `<div class="citations">${citations.map((item) => `<div class="citation" data-preview="${item.document_id}" data-page="${item.page}"><span class="citation-index">${item.index}</span><div><strong>${escapeHtml(item.document_name)}</strong><small>${escapeHtml(item.quote)}</small></div><span class="citation-page">第 ${item.page} 页 →</span></div>`).join("")}</div>` : "";
    const semanticBadge = semantic?.length ? `<span class="route-badge secondary">语义扩展 ${semantic.length}</span>` : "";
    node.innerHTML = `<div class="bubble"><div class="answer-meta"><span class="route-badge">${escapeHtml(route || "阅卷答复")}</span>${semanticBadge}${llmUsed ? '<span class="route-badge secondary">模型生成</span>' : ""}</div><div class="answer-text">${markdown(content)}</div>${citationHtml}</div>`;
  }
  $("#chat-messages").append(node);
  $$('[data-preview]', node).forEach((button) => button.addEventListener("click", () => openPage(Number(button.dataset.preview), Number(button.dataset.page))));
}

function appendTyping() {
  const node = document.createElement("div"); node.id = "typing-message"; node.className = "message assistant";
  node.innerHTML = '<div class="bubble"><span class="typing"><i></i><i></i><i></i></span></div>';
  $("#chat-messages").append(node); $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
}

async function sendChat(event) {
  event?.preventDefault();
  const input = $("#chat-input"), question = input.value.trim();
  if (!question || !state.caseId) return;
  if ($(".welcome-message")) $("#chat-messages").innerHTML = "";
  appendMessage("user", question); input.value = ""; appendTyping();
  const button = $(".send-button"); button.disabled = true;
  try {
    const result = await api(`/api/cases/${state.caseId}/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, user_name: $("#user-name").value || "本机律师", conversation_id: state.conversationId, use_llm: $("#llm-toggle").checked }) });
    $("#typing-message")?.remove(); state.conversationId = result.conversation_id;
    appendMessage("assistant", result.answer, result.route, result.citations, result.semantic_expansion, result.llm_used);
    $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
    state.conversations = await api(`/api/cases/${state.caseId}/conversations`); renderConversations();
  } catch (error) { $("#typing-message")?.remove(); appendMessage("assistant", `处理失败：${error.message}`, "系统提示"); }
  finally { button.disabled = false; }
}

async function uploadFiles(fileList) {
  const files = [...fileList]; if (!files.length || !state.caseId) return;
  const form = new FormData(); files.forEach((file) => form.append("files", file));
  toast(`正在解析 ${files.length} 个文件…`);
  try {
    const result = await api(`/api/cases/${state.caseId}/documents`, { method: "POST", body: form });
    if (result.failures.length) toast(`${result.failures.length} 个文件失败：${result.failures[0].error}`, "error");
    if (result.documents.length) toast(`已完成 ${result.documents.length} 个文件的拆页与索引`);
    await selectCase(state.caseId); showView("directory");
  } catch (error) { toast(error.message, "error"); }
  finally { $("#file-input").value = ""; }
}

function showView(name) {
  if (!state.caseId) { clearCaseWorkspace(); return; }
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${name}-view`));
  $$(".nav-item").forEach((item) => {
    const active = item.dataset.view === name;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  if (name === "lab") loadLabMetrics();
}

function bindEvents() {
  const menu = $("#workspace-menu");
  const toggle = $("#workspace-menu-toggle");
  const items = () => $$("[role=menuitem]", menu).filter((item) => !item.hidden && !item.disabled);
  const closeMenu = (restore = false) => {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    if (restore) toggle.focus();
  };
  const openMenu = (last = false) => {
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    const available = items();
    available[last ? available.length - 1 : 0]?.focus();
  };
  toggle.addEventListener("click", () => menu.hidden ? openMenu() : closeMenu(true));
  toggle.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      openMenu(event.key === "ArrowUp");
    }
  });
  menu.addEventListener("keydown", (event) => {
    const available = items();
    const index = available.indexOf(document.activeElement);
    let next;
    if (event.key === "ArrowDown") next = (index + 1) % available.length;
    if (event.key === "ArrowUp") next = (index - 1 + available.length) % available.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = available.length - 1;
    if (next !== undefined) { event.preventDefault(); available[next]?.focus(); }
    if (event.key === "Tab") closeMenu(true);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) { event.preventDefault(); closeMenu(true); }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest(".workspace-menu")) closeMenu();
  });
  document.addEventListener("focusin", (event) => {
    if (!event.target.closest(".workspace-menu")) closeMenu();
  });
  // Close before opening a dialog so its focus returns to the avatar.
  menu.addEventListener("click", (event) => {
    if (event.target.closest("[role=menuitem]")) closeMenu(true);
  }, true);
  $("#model-settings-btn").addEventListener("click", async () => {
    $("#model-dialog-state").textContent = "正在检查模型服务…";
    $("#model-dialog-name").textContent = "";
    $("#model-dialog-dot").classList.remove("online");
    $("#model-settings-dialog").showModal();
    try {
      const health = await api("/api/health");
      $("#model-dialog-state").textContent = health.local_llm ? "本地模型在线" : "规则检索模式";
      $("#model-dialog-name").textContent = health.model || "未返回模型名称";
      $("#model-dialog-dot").classList.toggle("online", Boolean(health.local_llm));
    } catch (error) {
      $("#model-dialog-state").textContent = "无法检查模型服务";
      $("#model-dialog-name").textContent = error.message;
    }
  });
  $("#model-settings-dialog").addEventListener("click", (event) => {
    const dialog = event.currentTarget;
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
  });
  $("#hero-review").addEventListener("click", () => showView("assistant"));
  $("#hero-upload").addEventListener("click", () => $("#file-input").click());
  $("#theme-toggle").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("lexvault-theme", theme);
    applyTheme(theme);
  });
  $$("[data-case-storage]").forEach((button) => button.addEventListener("click", () => openCaseStorage(button.dataset.caseStorage)));
  $$("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  $("#auth-retry").addEventListener("click", () => location.reload());
  $("#auth-reveal").addEventListener("click", () => {
    const visible = $("#auth-token").type === "password";
    $("#auth-token").type = visible ? "text" : "password";
    $("#auth-reveal").textContent = visible ? "隐藏" : "显示";
    $("#auth-reveal").setAttribute("aria-label", visible ? "隐藏访问令牌" : "显示访问令牌");
    $("#auth-reveal").setAttribute("aria-pressed", String(visible));
  });
  $("#auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = $("#auth-submit");
    if (submit.disabled) return;
    const token = $("#auth-token").value;
    submit.disabled = true;
    submit.textContent = "正在登录…";
    $("#auth-form").setAttribute("aria-busy", "true");
    $("#auth-error").textContent = "";
    $("#auth-token").removeAttribute("aria-invalid");
    try {
      await api("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
      $("#auth-token").value = "";
      location.reload();
    } catch (error) {
      $("#auth-error").textContent = error.status ? error.message : "连接失败，请检查网络后重试。";
      if (error.status === 401) {
        $("#auth-token").value = "";
        $("#auth-token").setAttribute("aria-invalid", "true");
      }
      $("#auth-token").focus();
    } finally {
      submit.disabled = false;
      submit.textContent = "登录工作空间 →";
      $("#auth-form").removeAttribute("aria-busy");
    }
  });
  $("#sign-out").addEventListener("click", async () => {
    try { await api("/api/auth/session", { method: "DELETE" }); sessionStorage.removeItem("lexvault-active-job"); location.reload(); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#annotation-form").addEventListener("submit", saveAnnotation);
  $("#annotation-reset").addEventListener("click", () => $("#annotation-form").reset());
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $("#file-input").addEventListener("change", (event) => uploadFiles(event.target.files));
  $("#directory-upload").addEventListener("click", () => $("#file-input").click());
  const zone = $("#upload-zone"); zone.addEventListener("click", () => $("#file-input").click());
  zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("dragging"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
  zone.addEventListener("drop", (event) => { event.preventDefault(); zone.classList.remove("dragging"); uploadFiles(event.dataTransfer.files); });
  $("#directory-search").addEventListener("input", (event) => { state.directoryQuery = event.target.value; renderDirectory(); });
  $$("#directory-filters button").forEach((button) => button.addEventListener("click", () => { state.directoryFilter = button.dataset.filter; $$("#directory-filters button").forEach((x) => x.classList.toggle("active", x === button)); renderDirectory(); }));
  $("#global-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { state.directoryQuery = event.target.value; $("#directory-search").value = event.target.value; showView("directory"); renderDirectory(); } });
  $("#save-directory").addEventListener("click", saveDirectory);
  $("#analyze-btn").addEventListener("click", runAnalysis);
  $("#run-agent-btn").addEventListener("click", runAgentLab);
  $("#compare-agent-btn").addEventListener("click", compareAgentRuntimes);
  $("#agent-runtime").addEventListener("change", (event) => {
    $("#run-agent-btn").innerHTML = `运行${event.target.value === "langgraph" ? "可恢复分析" : "标准分析"} <b>→</b>`;
  });
  $("#build-index-btn").addEventListener("click", buildHybridIndex);
  $("#evaluate-btn").addEventListener("click", runEvaluation);
  $("#new-chat").addEventListener("click", resetChat);
  $("#chat-form").addEventListener("submit", sendChat);
  $("#chat-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendChat(); } });
  $("#export-btn").addEventListener("click", () => {
    const templateId = $("#export-template").value;
    const query = templateId ? `?template_id=${encodeURIComponent(templateId)}` : "";
    window.location.href = `/api/cases/${state.caseId}/export${query}`;
    toast("正在生成案件审阅包");
  });
  $("#export-final-btn").addEventListener("click", async () => {
    const templateId = $("#export-template").value;
    const query = templateId ? `?template_id=${encodeURIComponent(templateId)}&final=true` : "?final=true";
    try {
      const response = await api(`/api/cases/${state.caseId}/export${query}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = decodeURIComponent((response.headers.get("content-disposition") || "").match(/filename="?([^";]+)"?/)?.[1] || "结案包.zip");
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      const sha = response.headers.get("x-package-sha256");
      toast(sha ? `结案包已生成，包哈希 ${sha.slice(0, 16)}（已写入审计）` : "结案包已生成（已写入审计）");
    } catch (error) {
      toast(error.message, "error");
    }
  });
  $("#new-case-btn").addEventListener("click", () => $("#case-dialog").showModal());
  $("#case-form").addEventListener("submit", createCase);
  loadExportTemplates();

  // 批量上传事件
  $("#batch-upload-btn").addEventListener("click", showBatchUpload);
  $("#batch-upload-close").addEventListener("click", hideBatchUpload);
  $("#batch-file-select").addEventListener("click", () => $("#batch-file-input").click());
  $("#batch-file-input").addEventListener("change", (event) => handleBatchUpload(event.target.files));

  // 证据编辑事件
  $("#save-evidence").addEventListener("click", saveEvidence);
  $("#create-evidence-btn").addEventListener("click", openEvidenceCreate);

  const batchDropArea = $("#batch-drop-area");
  batchDropArea.addEventListener("dragover", (event) => { event.preventDefault(); batchDropArea.classList.add("dragging"); });
  batchDropArea.addEventListener("dragleave", () => batchDropArea.classList.remove("dragging"));
  batchDropArea.addEventListener("drop", (event) => {
    event.preventDefault();
    batchDropArea.classList.remove("dragging");
    handleBatchUpload(event.dataTransfer.files);
  });
}

async function createCase(event) {
  event.preventDefault();
  const body = { title: $("#new-case-title").value, case_no: $("#new-case-no").value, case_type: $("#new-case-type").value, client_name: $("#new-case-client").value, description: $("#new-case-description").value };
  try {
    const created = await api("/api/cases", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#case-dialog").close(); $("#case-form").reset(); await loadCases(); await selectCase(created.id); toast("案件空间已创建");
  } catch (error) { toast(error.message, "error"); }
}

// ==================== 批量上传功能 ====================

function showBatchUpload() {
  $("#batch-upload-zone").style.display = "block";
  $("#batch-progress").style.display = "none";
  $("#batch-errors").style.display = "none";
}

function hideBatchUpload() {
  $("#batch-upload-zone").style.display = "none";
  $("#batch-file-input").value = "";
}

async function handleBatchUpload(files) {
  if (!files || files.length === 0) {
    toast("未选择任何文件", "error");
    return;
  }

  if (files.length > 200) {
    toast("批量导入最多支持 200 个文件", "error");
    return;
  }

  try {
    // 显示进度
    $("#batch-drop-area").style.display = "none";
    $("#batch-progress").style.display = "block";
    $("#batch-progress-text").textContent = `0 / ${files.length}`;
    $("#batch-progress-fill").style.width = "0%";
    $("#batch-success-count").textContent = "0";
    $("#batch-failed-count").textContent = "0";

    // 提交文件
    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file);
    }

    const result = await api(`/api/cases/${state.caseId}/batch-import`, {
      method: "POST",
      body: formData
    });

    toast(`批量导入任务已提交，共 ${result.total_files} 个文件`, "success");

    // 轮询进度
    pollBatchStatus(result.batch_id);

  } catch (error) {
    toast(error.message, "error");
    $("#batch-drop-area").style.display = "block";
    $("#batch-progress").style.display = "none";
  }
}

async function pollBatchStatus(batchId) {
  const pollInterval = setInterval(async () => {
    try {
      const status = await api(`/api/batch-imports/${batchId}`);

      // 更新进度 UI
      $("#batch-progress-text").textContent = `${status.processed_files} / ${status.total_files}`;
      $("#batch-progress-fill").style.width = `${status.progress_percent}%`;
      $("#batch-success-count").textContent = status.successful_files;
      $("#batch-failed-count").textContent = status.failed_files;

      // 显示错误
      if (status.error_log && status.error_log.length > 0) {
        $("#batch-errors").style.display = "block";
        $("#batch-errors").innerHTML = `<strong>错误日志:</strong><ul>${status.error_log.slice(0, 5).map(err => `<li>${escapeHtml(err)}</li>`).join("")}</ul>`;
      }

      // 检查是否完成
      if (status.status === "completed" || status.status === "completed_with_errors" || status.status === "failed") {
        clearInterval(pollInterval);

        if (status.status === "completed") {
          toast(`批量导入完成！成功 ${status.successful_files} 个文件`, "success");
        } else if (status.status === "completed_with_errors") {
          toast(`批量导入完成，成功 ${status.successful_files}，失败 ${status.failed_files}`, "warning");
        } else {
          toast(`批量导入失败`, "error");
        }

        // 刷新文档列表
        await selectCase(state.caseId);

        // 3秒后隐藏进度区域
        setTimeout(() => {
          hideBatchUpload();
        }, 3000);
      }

    } catch (error) {
      clearInterval(pollInterval);
      toast(`查询进度失败: ${error.message}`, "error");
    }
  }, 1000);
}

// 渲染证据时间线
function renderEvidenceTimeline() {
  if (!state.evidence || state.evidence.length === 0) {
    $("#evidence-timeline").innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ba5a2;">暂无证据数据</div>';
    return;
  }

  if (typeof echarts === 'undefined') {
    $("#evidence-timeline").innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ba5a2;">可视化库加载中...</div>';
    return;
  }

  // 从文档中获取时间信息并关联到证据
  const documentMap = new Map(state.documents.map(d => [d.id, d]));
  const evidenceWithTime = state.evidence
    .map(e => {
      const doc = documentMap.get(e.source_document_id);
      if (doc && doc.date_range && doc.date_range.trim()) {
        // 尝试解析日期范围(格式如 "2023-01-15" 或 "2023-01-15 至 2023-02-20")
        const dateMatch = doc.date_range.match(/(\d{4}-\d{2}-\d{2})/);
        if (dateMatch) {
          return {
            title: e.title,
            date: dateMatch[1],
            category: e.category,
            status: e.status,
            docName: doc.name
          };
        }
      }
      return null;
    })
    .filter(e => e !== null)
    .sort((a, b) => a.date.localeCompare(b.date));

  if (evidenceWithTime.length === 0) {
    $("#evidence-timeline").innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ba5a2;">证据所在文档缺少时间信息</div>';
    return;
  }

  const chart = echarts.init(document.getElementById('evidence-timeline'));

  // 按日期聚合证据数量
  const dateCountMap = {};
  evidenceWithTime.forEach(e => {
    dateCountMap[e.date] = (dateCountMap[e.date] || 0) + 1;
  });

  const timelineData = Object.entries(dateCountMap)
    .map(([date, count]) => [date, count])
    .sort((a, b) => a[0].localeCompare(b[0]));

  const option = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: function(params) {
        const date = params[0].axisValue;
        const count = params[0].value[1];
        return `日期：${date}<br/>证据数量：${count}`;
      }
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      top: '10%',
      containLabel: true
    },
    xAxis: {
      type: 'time',
      boundaryGap: false,
      axisLabel: {
        formatter: '{yyyy}-{MM}-{dd}',
        fontSize: 10
      }
    },
    yAxis: {
      type: 'value',
      name: '证据数量',
      minInterval: 1,
      axisLabel: {
        fontSize: 10
      }
    },
    series: [
      {
        name: '证据',
        type: 'line',
        smooth: true,
        data: timelineData,
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(12, 118, 93, 0.3)' },
            { offset: 1, color: 'rgba(12, 118, 93, 0.05)' }
          ])
        },
        itemStyle: {
          color: '#0c765d'
        },
        lineStyle: {
          color: '#0c765d',
          width: 2
        }
      }
    ]
  };

  chart.setOption(option);

  // 响应式调整
  window.addEventListener('resize', () => chart.resize());
}

// 渲染疏漏检测仪表板
async function renderGapDashboard() {
  const container = $('#gap-dashboard');

  // 初始化空状态
  $('#gap-summary-grid').innerHTML = `
    <div class="gap-summary-card">
      <span class="gap-label">总疏漏</span>
      <strong class="gap-count">-</strong>
      <span class="gap-label">待检测</span>
    </div>
    <div class="gap-summary-card severity-high">
      <span class="gap-label">高严重度</span>
      <strong class="gap-count">-</strong>
      <span class="gap-label">需立即处理</span>
    </div>
    <div class="gap-summary-card severity-medium">
      <span class="gap-label">中严重度</span>
      <strong class="gap-count">-</strong>
      <span class="gap-label">需关注</span>
    </div>
    <div class="gap-summary-card severity-low">
      <span class="gap-label">低严重度</span>
      <strong class="gap-count">-</strong>
      <span class="gap-label">建议优化</span>
    </div>
  `;

  $('#gap-details-list').innerHTML = '<div style="padding: 20px; text-align: center; color: #9ba5a2;">点击"🔍 运行疏漏检测"开始分析</div>';

  // 绑定运行疏漏检测按钮
  $('#run-gap-detection-btn').onclick = runGapDetection;
}

// 执行疏漏检测
async function runGapDetection() {
  if (!state.caseId) {
    toast('请先选择案件', 'error');
    return;
  }

  try {
    $('#run-gap-detection-btn').disabled = true;
    $('#run-gap-detection-btn').textContent = '🔄 检测中...';

    // 调用疏漏检测 API
    const result = await api(`/api/cases/${state.caseId}/gap-analysis`);

    if (!result || !result.gaps) {
      toast('未检测到疏漏信息', 'success');
      return;
    }

    const gaps = result.gaps;

    // 统计各严重度数量
    const stats = result.statistics || result.summary || {};
    const severityCounts = {
      '高': stats.high || 0,
      '中': stats.medium || 0,
      '低': stats.low || 0
    };

    const totalGaps = stats.total || gaps.length;

    // 更新汇总卡片
    $('#gap-summary-grid').innerHTML = `
      <div class="gap-summary-card">
        <span class="gap-label">总疏漏</span>
        <strong class="gap-count">${totalGaps}</strong>
        <span class="gap-label">已检测</span>
      </div>
      <div class="gap-summary-card severity-high">
        <span class="gap-label">高严重度</span>
        <strong class="gap-count">${severityCounts['高']}</strong>
        <span class="gap-label">需立即处理</span>
      </div>
      <div class="gap-summary-card severity-medium">
        <span class="gap-label">中严重度</span>
        <strong class="gap-count">${severityCounts['中']}</strong>
        <span class="gap-label">需关注</span>
      </div>
      <div class="gap-summary-card severity-low">
        <span class="gap-label">低严重度</span>
        <strong class="gap-count">${severityCounts['低']}</strong>
        <span class="gap-label">建议优化</span>
      </div>
    `;

    // 渲染详细列表
    if (gaps.length === 0) {
      $('#gap-details-list').innerHTML = '<div style="padding: 20px; text-align: center; color: #10b981;">本次检测未发现疏漏，仍需结合案情人工复核。</div>';
    } else {
      $('#gap-details-list').innerHTML = gaps.map(gap => `
        <div class="gap-item severity-${escapeHtml(gap.severity)}">
          <span class="gap-type-badge">${escapeHtml(gap.gap_type)}</span>
          <div class="gap-content">
            <strong>${escapeHtml(gap.description)}</strong>
            <p>${escapeHtml(gap.details || '')}</p>
            ${gap.suggestion ? `<div class="gap-suggestion">💡 建议：${escapeHtml(gap.suggestion)}</div>` : ''}
          </div>
          <span class="gap-severity">严重度：${escapeHtml(gap.severity)}</span>
        </div>
      `).join('');
    }

    toast(`疏漏检测完成，发现 ${totalGaps} 处需要关注的问题`, totalGaps === 0 ? 'success' : 'warning');

  } catch (error) {
    console.error('疏漏检测失败:', error);
    toast(`疏漏检测失败: ${error.message}`, 'error');
    $('#gap-details-list').innerHTML = '<div style="padding: 20px; text-align: center; color: #dc6a58;">检测失败，请稍后重试</div>';
  } finally {
    $('#run-gap-detection-btn').disabled = false;
    $('#run-gap-detection-btn').textContent = '🔍 运行疏漏检测';
  }
}

// ========================================
// 证据编辑功能
// ========================================

/**
 * 填充来源文档下拉选项
 */
function populateDocumentOptions() {
  const select = $("#edit-evidence-source");
  select.innerHTML = '<option value="">无来源</option>' +
    state.documents.map(doc =>
      `<option value="${doc.id}">${escapeHtml(doc.name)}</option>`
    ).join("");
}

/**
 * 打开证据创建对话框
 */
function openEvidenceCreate() {
  $("#evidence-dialog h3").textContent = "新建证据";
  $("#edit-evidence-id").value = "";

  // 重置表单
  $("#edit-evidence-title").value = "";
  $("#edit-evidence-category").value = "书证";
  $("#edit-evidence-fact").value = "";
  $("#edit-evidence-credibility").value = "待核验";
  $("#edit-evidence-status").value = "待复核";
  $("#edit-evidence-source").value = "";
  $("#edit-evidence-page-start").value = "1";
  $("#edit-evidence-page-end").value = "1";
  $("#edit-evidence-quote").value = "";

  // 填充来源文档选项
  populateDocumentOptions();

  $("#evidence-dialog").showModal();
}

/**
 * 打开证据编辑对话框
 * @param {number} evidenceId - 证据 ID
 */
function openEvidenceEdit(evidenceId) {
  const evidence = state.evidence.find(e => e.id === evidenceId);
  if (!evidence) {
    toast("证据不存在", "error");
    return;
  }

  $("#evidence-dialog h3").textContent = "编辑证据";
  $("#edit-evidence-id").value = evidence.id;

  // 填充表单
  $("#edit-evidence-title").value = evidence.title;
  $("#edit-evidence-category").value = evidence.category;
  $("#edit-evidence-fact").value = evidence.fact;
  $("#edit-evidence-credibility").value = evidence.credibility;
  $("#edit-evidence-status").value = evidence.status;
  $("#edit-evidence-source").value = evidence.source_document_id || "";
  $("#edit-evidence-page-start").value = evidence.source_page_start;
  $("#edit-evidence-page-end").value = evidence.source_page_end;
  $("#edit-evidence-quote").value = evidence.quote;

  // 填充来源文档选项
  populateDocumentOptions();

  $("#evidence-dialog").showModal();
}

/**
 * 保存证据(创建或更新)
 */
async function saveEvidence() {
  const evidenceId = $("#edit-evidence-id").value;
  const isCreate = !evidenceId;

  // 收集表单数据
  const body = {
    title: $("#edit-evidence-title").value.trim(),
    category: $("#edit-evidence-category").value,
    fact: $("#edit-evidence-fact").value.trim(),
    credibility: $("#edit-evidence-credibility").value,
    status: $("#edit-evidence-status").value,
    source_document_id: $("#edit-evidence-source").value ?
      Number($("#edit-evidence-source").value) : null,
    source_page_start: Number($("#edit-evidence-page-start").value),
    source_page_end: Number($("#edit-evidence-page-end").value),
    quote: $("#edit-evidence-quote").value.trim()
  };

  // 前端验证
  if (!body.title || body.title.length < 2) {
    toast("证据标题至少需要 2 个字符", "error");
    $("#edit-evidence-title").focus();
    return;
  }

  if (!body.fact) {
    toast("待证事实不能为空", "error");
    $("#edit-evidence-fact").focus();
    return;
  }

  if (body.source_page_start < 1) {
    toast("起始页码必须大于等于 1", "error");
    $("#edit-evidence-page-start").focus();
    return;
  }

  if (body.source_page_end < body.source_page_start) {
    toast("结束页码不能小于起始页码", "error");
    $("#edit-evidence-page-end").focus();
    return;
  }

  try {
    $("#save-evidence").disabled = true;
    $("#save-evidence").textContent = "保存中...";

    if (isCreate) {
      // 创建
      await api(`/api/cases/${state.caseId}/evidence`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      toast("证据创建成功", "success");
    } else {
      // 更新
      await api(`/api/evidence/${evidenceId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      toast("证据更新成功", "success");
    }

    $("#evidence-dialog").close();
    await selectCase(state.caseId); // 刷新数据

  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("#save-evidence").disabled = false;
    $("#save-evidence").textContent = "保存证据";
  }
}

/**
 * 确认删除证据
 * @param {number} evidenceId - 证据 ID
 */
function confirmDeleteEvidence(evidenceId) {
  const evidence = state.evidence.find(e => e.id === evidenceId);
  if (!evidence) {
    toast("证据不存在", "error");
    return;
  }

  // 统计关联
  const relationsCount = state.relations.filter(r =>
    r.from_evidence_id === evidenceId || r.to_evidence_id === evidenceId
  ).length;

  $("#delete-confirm-message").innerHTML = `
    确定要删除证据「<strong>${escapeHtml(evidence.title)}</strong>」吗？<br><br>
    ${relationsCount > 0 ? `<span style="color: #dc6a58;">⚠️ 将同时删除 ${relationsCount} 条证据关联关系</span>` : '此操作不可恢复'}
  `;

  // 绑定确认按钮(移除旧监听器防止重复绑定)
  const confirmBtn = $("#confirm-delete-evidence");
  const newBtn = confirmBtn.cloneNode(true);
  confirmBtn.replaceWith(newBtn);

  newBtn.onclick = async () => {
    await deleteEvidence(evidenceId);
    $("#delete-confirm-dialog").close();
  };

  $("#delete-confirm-dialog").showModal();
}

/**
 * 执行删除证据操作
 * @param {number} evidenceId - 证据 ID
 */
async function deleteEvidence(evidenceId) {
  try {
    const result = await api(`/api/evidence/${evidenceId}`, {
      method: "DELETE"
    });

    toast(result.message, "success");
    await selectCase(state.caseId); // 刷新数据

  } catch (error) {
    toast(error.message, "error");
  }
}

// ========================================
// 键盘快捷键支持
// ========================================

/**
 * 注册全局键盘快捷键
 */
function registerKeyboardShortcuts() {
  document.addEventListener("keydown", (e) => {
    // ESC: 关闭所有对话框
    if (e.key === "Escape") {
      const dialogs = $$("dialog[open]");
      dialogs.forEach(dialog => dialog.close());
      return;
    }

    // Ctrl/Cmd + K: 聚焦搜索框
    if ((e.ctrlKey || e.metaKey) && e.key === "k") {
      e.preventDefault();
      const searchInput = $("#directory-search");
      if (searchInput) {
        searchInput.focus();
        searchInput.select();
      }
      return;
    }

    // Ctrl/Cmd + N: 新建证据(仅在证据视图)
    if ((e.ctrlKey || e.metaKey) && e.key === "n" && $("#evidence-view").style.display !== "none") {
      e.preventDefault();
      openEvidenceCreate();
      return;
    }

    // Ctrl/Cmd + Enter: 发送问答消息
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      const chatInput = $("#chat-input");
      if (chatInput && document.activeElement === chatInput && chatInput.value.trim()) {
        e.preventDefault();
        sendMessage();
      }
      return;
    }
  });
}

// ========================================
// 应用启动
// ========================================

document.addEventListener("DOMContentLoaded", () => {
  initializeTheme();
  bootstrap();
  registerKeyboardShortcuts();
});
