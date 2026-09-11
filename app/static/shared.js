/* Shared transport has no UI or authentication storage side effects. */
(() => {
  "use strict";
  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", ...options });
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch { /* Non-JSON response. */ }
      let message = `请求失败(${response.status})`;
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) message = detail.map((item) => item.msg || "参数无效").join("；");
      else if (detail?.message) message = detail.message;
      const error = new Error(message);
      error.status = response.status;
      error.runId = detail?.run_id;
      error.resumable = Boolean(detail?.resumable);
      if (response.status === 401 && (!path.startsWith("/api/auth/") || options.method === "DELETE")) window.dispatchEvent(new CustomEvent("lexvault:unauthorized", { detail: { path, status: 401, error } }));
      throw error;
    }
    return (response.headers.get("content-type") || "").includes("application/json") ? response.json() : response;
  }
  function escapeHtml(value = "") {
    return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
  }
  function markdown(value = "") {
    const output = [];
    let inList = false;
    for (const raw of escapeHtml(value).split("\n")) {
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
  function formatDate(value) {
    if (!value) return "刚刚";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function statusClass(status = "") { return /确认|校准|完成/.test(status) ? "confirmed" : /待/.test(status) ? "pending" : ""; }
  window.LexVault = Object.assign(window.LexVault || {}, { api, escapeHtml, markdown, formatDate, statusClass });
})();
