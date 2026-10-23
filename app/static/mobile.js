/* Mobile client: case and view epochs prevent stale reads from painting another case. */
(() => {
  "use strict";
  const { api, escapeHtml: esc, markdown, formatDate, statusClass } = window.LexVault;
  const $ = (selector, root = document) => root.querySelector(selector);
  const state = { identity: null, cases: [], caseId: null, epoch: 0, viewEpoch: 0, sheetEpoch: 0, view: "overview", documents: [], evidence: [], relations: [], conversationId: null, chatEpoch: 0 };
  const json = (body, method = "POST") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const allowed = (permission) => Boolean(state.identity?.authenticated && (state.identity.admin || state.identity.permissions?.includes(permission)));
  const disabled = (permission) => allowed(permission) ? "" : 'disabled title="当前账号无此操作权限"';
  const empty = (text = "暂无记录") => `<p class="empty">${esc(text)}</p>`;
  const context = () => ({ caseId: state.caseId, epoch: state.epoch, viewEpoch: state.viewEpoch });
  const current = (ctx) => state.identity?.authenticated && ctx.caseId === state.caseId && ctx.epoch === state.epoch;
  const visible = (ctx) => current(ctx) && ctx.viewEpoch === state.viewEpoch;
  const casePath = (ctx, suffix) => `/api/cases/${ctx.caseId}/${suffix}`;
  function notice(message) { $("#notice").textContent = message; $("#notice").hidden = !message; }
  function fail(error, ctx) { if (!ctx || current(ctx)) notice(error.message || "网络连接失败，请重试。"); }
  function on(selector, event, handler, root = document) { const node = $(selector, root); if (node) node.addEventListener(event, handler); }
  let sheetSerial = 0;
  const sheetHistory = [];
  function sheet(title, html) {
    if ($("#sheet").open) {
      const fragment = document.createDocumentFragment();
      while ($("#sheet-body").firstChild) fragment.append($("#sheet-body").firstChild);
      sheetHistory.push({ title: $("#sheet-title").textContent, fragment, epoch: state.sheetEpoch });
    }
    state.sheetEpoch = ++sheetSerial;
    $("#sheet-title").textContent = title; $("#sheet-body").innerHTML = html; $("#sheet-error").textContent = "";
    if (!$("#sheet-back")) {
      const back = document.createElement("button"); back.id = "sheet-back"; back.type = "button"; back.className = "icon"; back.textContent = "‹"; back.setAttribute("aria-label", "返回上一层"); back.title = "返回上一层"; back.onclick = backSheet; $("#sheet-title").before(back);
    }
    $("#sheet-back").hidden = !sheetHistory.length;
    if (!$("#sheet").open) $("#sheet").showModal();
    return state.sheetEpoch;
  }
  function closeSheet() { state.sheetEpoch = ++sheetSerial; sheetHistory.length = 0; $("#sheet").close(); }
  function backSheet() {
    const previous = sheetHistory.pop();
    if (!previous) { closeSheet(); return; }
    state.sheetEpoch = previous.epoch; $("#sheet-title").textContent = previous.title; $("#sheet-body").replaceChildren(previous.fragment); $("#sheet-error").textContent = ""; $("#sheet-back").hidden = !sheetHistory.length;
  }
  const sheetCurrent = (ctx, seq) => current(ctx) && seq === state.sheetEpoch && $("#sheet").open;
  function field(name, label, value = "", extra = "") { return `<label>${label}<input name="${name}" value="${esc(value ?? "")}" ${extra}></label>`; }
  function area(name, label, value = "", extra = "") { return `<label>${label}<textarea name="${name}" ${extra}>${esc(value ?? "")}</textarea></label>`; }
  function select(name, label, values, value) { return `<label>${label}<select name="${name}">${values.map(([id, title]) => `<option value="${esc(id)}" ${String(id) === String(value) ? "selected" : ""}>${esc(title)}</option>`).join("")}</select></label>`; }
  const choices = (items) => items.map((x) => [x, x]);
  function bindForm(id, ctx, permission, save, done) {
    const form = $(id), seq = state.sheetEpoch;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!current(ctx) || !allowed(permission) || form.dataset.busy) return;
      form.dataset.busy = "true"; const button = form.querySelector('[type="submit"]'); if (button) button.disabled = true;
      try {
        const result = await save(Object.fromEntries(new FormData(form)));
        if (sheetCurrent(ctx, seq)) await done(result);
      } catch (error) { if (sheetCurrent(ctx, seq)) $("#sheet-error").textContent = error.message; }
      finally { delete form.dataset.busy; if (button) button.disabled = !allowed(permission); }
    });
  }
  function expire() {
    if (!state.identity?.authenticated) return;
    state.identity = null; state.epoch += 1; state.chatEpoch += 1;
    state.documents = []; state.evidence = []; state.relations = []; closeSheet();
    $("#workspace").hidden = true; $("#content").replaceChildren(); $("#auth").hidden = false;
    $("#auth-error").textContent = "登录已过期，请重新认证。";
    checkIdentity(false);
  }
  window.addEventListener("lexvault:unauthorized", expire);
  async function checkIdentity(retry = true) {
    $("#auth-status").textContent = "正在检查身份…";
    try {
      const identity = await api("/api/auth/me"); state.identity = identity;
      $("#support").textContent = identity.support_contact || "请联系律所管理员";
      $("#auth-status").textContent = identity.organization_name || "";
      $("#login").hidden = identity.authenticated; $("#sso").hidden = identity.authenticated || !identity.oidc_enabled;
      if (!identity.authenticated) return;
      $("#auth").hidden = true; $("#workspace").hidden = false;
      const cases = await api("/api/cases");
      if (state.identity !== identity) return;
      state.cases = cases;
      $("#case-select").innerHTML = cases.map((item) => `<option value="${Number(item.id)}">${esc(item.title)}</option>`).join("") || '<option value="">暂无案件</option>';
      await chooseCase(cases.some((item) => item.id === state.caseId) ? state.caseId : cases[0]?.id);
    } catch (error) {
      if (retry && (!error.status || error.status >= 500)) { await checkIdentity(false); return; }
      $("#auth-status").textContent = "连接失败"; $("#auth-error").textContent = error.message;
      if (state.identity?.authenticated) notice(error.message);
    }
  }
  async function chooseCase(id) {
    state.caseId = id ? Number(id) : null; state.epoch += 1; state.chatEpoch += 1; state.conversationId = null;
    state.documents = []; state.evidence = []; state.relations = []; closeSheet(); notice("");
    $("#case-select").value = state.caseId || "";
    await showView(state.view);
  }
  async function showView(view) {
    if (!["overview", "directory", "evidence", "chat", "tasks", "export"].includes(view)) return;
    state.view = view; state.viewEpoch += 1; state.chatEpoch += 1; closeSheet();
    document.querySelectorAll("[data-view]").forEach((button) => { if (button.dataset.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
    $("#content").innerHTML = empty("加载中…"); $("#main").scrollTop = 0;
    if (!state.caseId) { $("#content").innerHTML = empty("暂无可访问案件"); return; }
    const ctx = context();
    try { await ({ overview: overview, directory: directory, evidence: evidenceList, chat: chatView, tasks: tasks, export: exportsView })[view](ctx); }
    catch (error) { if (visible(ctx)) { $("#content").innerHTML = `<p class="error">${esc(error.message)}</p><button id="reload">重试</button>`; on("#reload", "click", () => showView(view)); } }
  }
  function uploadControl(ctx) {
    on("#upload", "change", async (event) => {
      const input = event.target, files = [...input.files]; if (!files.length || !current(ctx) || !allowed("edit")) return;
      input.disabled = true; const data = new FormData(); files.forEach((file) => data.append("files", file)); notice(`正在上传 ${files.length} 个文件…`);
      try {
        const result = await api(casePath(ctx, "documents"), { method: "POST", body: data });
        if (!current(ctx)) return;
        notice(`已完成 ${result.documents.length} 个文件。${(result.failures || []).map((item) => `${item.name}：${item.error}`).join("\n")}`);
        if (visible(ctx)) await showView("directory");
      } catch (error) { fail(error, ctx); } finally { input.value = ""; input.disabled = !allowed("edit"); }
    });
  }
  const uploadHtml = () => `<label class="button file-picker">选择卷宗文件<input id="upload" class="visually-hidden" aria-label="选择卷宗文件" type="file" multiple accept=".pdf,.docx,.txt,.md,.csv,.json,.png,.jpg,.jpeg,.tif,.tiff,.webp" ${disabled("edit")}></label>`;
  async function overview(ctx) {
    const [item, audit] = await Promise.all([api(`/api/cases/${ctx.caseId}`), api(casePath(ctx, "audit"))]);
    if (!visible(ctx)) return;
    $("#content").innerHTML = `<h1>${esc(item.title)}</h1><p class="muted">${esc(item.case_no)} · ${esc(item.case_type)} · ${esc(item.status)}</p><p>${esc(item.description || "")}</p><div class="metrics">${[["卷宗文件", "documents"], ["内容页", "pages"], ["证据事项", "evidence"], ["已确认", "confirmed"]].map(([label, key]) => `<div>${label}<strong>${Number(item.metrics[key])}</strong></div>`).join("")}</div><section class="section">${uploadHtml()}</section><h2>最近活动</h2>${audit.slice(0, 20).map((row) => `<article class="item"><strong>${esc(row.action)}</strong><p>${esc(row.detail)}</p><time>${esc(formatDate(row.created_at))}</time></article>`).join("") || empty()}`;
    uploadControl(ctx);
  }
  async function directory(ctx) {
    const docs = await api(casePath(ctx, "documents")); if (!visible(ctx)) return; state.documents = docs;
    $("#content").innerHTML = `<h1>卷宗目录</h1>${uploadHtml()}<label>搜索卷宗<input id="document-search" type="search" placeholder="文件、人员、日期、摘要"></label><div id="documents"></div>`;
    const render = (query) => {
      $("#documents").innerHTML = docs.filter((doc) => `${doc.name} ${doc.people} ${doc.date_range} ${doc.summary} ${doc.doc_type}`.toLowerCase().includes(query.toLowerCase())).map((doc) => `<button class="row" data-document="${Number(doc.id)}"><strong>${esc(doc.name)}</strong><small>${esc(doc.doc_type)} · ${Number(doc.pages)} 页 · ${esc(doc.people || "人员待校准")}</small><p>${esc(doc.summary)}</p><span class="badge ${statusClass(doc.status)}">${esc(doc.status)}</span></button>`).join("") || empty("没有匹配卷宗");
      document.querySelectorAll("[data-document]").forEach((button) => button.onclick = () => documentDetail(ctx, docs.find((doc) => doc.id === Number(button.dataset.document))));
    };
    render(""); on("#document-search", "input", (event) => render(event.target.value)); uploadControl(ctx);
  }
  function documentDetail(ctx, doc) {
    if (!current(ctx) || !doc) return;
    sheet(doc.name, `<dl><dt>类型</dt><dd>${esc(doc.doc_type)}</dd><dt>人员</dt><dd>${esc(doc.people)}</dd><dt>时间</dt><dd>${esc(doc.date_range)}</dd><dt>页数</dt><dd>${Number(doc.pages)}</dd></dl><p>${esc(doc.summary)}</p><div class="actions"><button id="read-page" ${doc.pages ? "" : "disabled"}>原文</button><button id="original">下载原文件</button><button id="calibrate" ${disabled("edit")}>校准目录</button></div>`);
    on("#read-page", "click", () => openPage(ctx, doc.id, 1, doc.pages)); on("#original", "click", () => download(ctx, `/api/documents/${doc.id}/file`, doc.name));
    on("#calibrate", "click", () => {
      sheet("校准目录", `<form id="directory-form"><fieldset ${disabled("edit")}>${field("doc_type", "文书类型", doc.doc_type)}${field("people", "涉及人员", doc.people)}${field("date_range", "时间范围", doc.date_range)}${select("status", "状态", choices(["已索引", "待复核", "已校准"]), doc.status)}${area("summary", "摘要", doc.summary)}<button type="submit" class="primary">保存目录</button></fieldset></form>`);
      bindForm("#directory-form", ctx, "edit", (body) => api(`/api/documents/${doc.id}/directory`, json(body, "PATCH")), async () => { closeSheet(); await showView("directory"); notice("目录已保存"); });
    });
  }
  async function openPage(ctx, documentId, pageNo, total) {
    if (!current(ctx) || !Number.isInteger(Number(documentId)) || Number(documentId) < 1) return;
    total = total || state.documents.find((doc) => doc.id === Number(documentId))?.pages;
    pageNo = Math.max(1, Math.min(total || Infinity, Number(pageNo) || 1));
    const seq = sheet("卷宗原文", empty("加载原页…"));
    try {
      const page = await api(`/api/documents/${Number(documentId)}/pages/${pageNo}`); if (!sheetCurrent(ctx, seq)) return;
      $("#sheet-title").textContent = page.name;
      $("#sheet-body").innerHTML = `<div class="page-controls"><button id="prev-page" class="icon" aria-label="上一页" ${pageNo <= 1 ? "disabled" : ""}>‹</button><label>第 ${pageNo} 页${total ? ` / ${Number(total)}` : ""}<input id="page-number" aria-label="跳转页码" type="number" min="1" ${total ? `max="${Number(total)}"` : ""} value="${pageNo}"></label><button id="next-page" class="icon" aria-label="下一页" ${total && pageNo >= total ? "disabled" : ""}>›</button></div><pre>${esc(page.text || "该页未识别到文本")}</pre><button id="original">下载原文件</button>`;
      on("#prev-page", "click", () => openPage(ctx, documentId, pageNo - 1, total)); on("#next-page", "click", () => openPage(ctx, documentId, pageNo + 1, total));
      on("#page-number", "change", (event) => { if (event.target.checkValidity()) openPage(ctx, documentId, Number(event.target.value), total); });
      on("#original", "click", () => download(ctx, `/api/documents/${Number(documentId)}/file`, page.name));
    } catch (error) { if (sheetCurrent(ctx, seq)) $("#sheet-body").innerHTML = `<p class="error">${esc(error.message)}</p><button id="page-retry">重试</button>`; on("#page-retry", "click", () => openPage(ctx, documentId, pageNo, total)); }
  }
  function citations(items = []) { return items.map((item) => `<button class="citation" data-source="${Number(item.document_id)}" data-page="${Number(item.page)}">资料 ${Number(item.index)} · ${esc(item.document_name)} · 第 ${Number(item.page)} 页<small>${esc(item.quote || "")}</small></button>`).join(""); }
  function bindCitations(ctx, root = document) { root.querySelectorAll("[data-source]").forEach((button) => button.onclick = () => openPage(ctx, Number(button.dataset.source), Number(button.dataset.page))); }
  async function evidenceList(ctx) {
    const [data, docs] = await Promise.all([api(casePath(ctx, "evidence")), api(casePath(ctx, "documents"))]); if (!visible(ctx)) return;
    state.evidence = data.evidence; state.relations = data.relations; state.documents = docs;
    $("#content").innerHTML = `<div class="heading"><h1>证据事项</h1><button id="add-evidence" ${disabled("edit")}>添加证据</button></div><div class="actions"><button id="analyze" ${disabled("edit")}>分析证据</button><button id="graph">关系图谱</button><button id="timeline">时间线</button><button id="gaps">疏漏</button></div><label>搜索证据<input id="evidence-search" type="search"></label><div id="evidence-list"></div>`;
    const render = (query) => { $("#evidence-list").innerHTML = data.evidence.filter((item) => `${item.title} ${item.fact} ${item.status}`.includes(query)).map((item) => `<button class="row" data-evidence="${Number(item.id)}"><strong>${esc(item.title)}</strong><small>${esc(item.category)} · ${esc(item.credibility)} · ${esc(item.status)}</small><p>${esc(item.fact)}</p></button>`).join("") || empty("暂无证据事项"); bindEvidence(ctx); };
    render(""); on("#evidence-search", "input", (event) => render(event.target.value));
    on("#add-evidence", "click", () => evidenceEdit(ctx)); on("#analyze", "click", (event) => caseAction(ctx, event.currentTarget, "analyze", () => showView("evidence")));
    on("#graph", "click", () => { sheet("证据关系图谱", relationHtml(state.relations)); bindEvidence(ctx); });
    on("#timeline", "click", () => {
      const rows = data.evidence.map((item) => ({ item, date: docs.find((doc) => doc.id === item.source_document_id)?.date_range || "" })).sort((a, b) => a.date.localeCompare(b.date));
      sheet("证据时间线", `<div class="timeline">${rows.map(({ item, date }) => `<button class="row" data-evidence="${Number(item.id)}"><small>${esc(date || "时间待校准")}</small><strong>${esc(item.title)}</strong></button>`).join("") || empty()}</div>`); bindEvidence(ctx);
    });
    on("#gaps", "click", () => gaps(ctx));
  }
  function bindEvidence(ctx) { document.querySelectorAll("[data-evidence]").forEach((button) => button.onclick = () => evidenceDetail(ctx, Number(button.dataset.evidence))); }
  function relationHtml(rows) {
    const title = (id) => state.evidence.find((item) => item.id === id)?.title || `证据 #${id}`;
    return rows.map((row) => `<article class="item"><div class="relation"><button data-evidence="${Number(row.from_evidence_id)}">${esc(title(row.from_evidence_id))}</button><span>→</span><button data-evidence="${Number(row.to_evidence_id)}">${esc(title(row.to_evidence_id))}</button></div><p><span class="badge">${esc(row.relation_type)}</span> ${esc(row.note)}</p></article>`).join("") || empty("暂无关联关系");
  }
  function evidenceDetail(ctx, id) {
    if (!current(ctx)) return; const item = state.evidence.find((row) => row.id === id); if (!item) return;
    sheet(item.title, `<p>${esc(item.category)} · ${esc(item.credibility)} · ${esc(item.status)}</p><h3>待证事实</h3><p>${esc(item.fact)}</p><blockquote>${esc(item.quote || "暂无原文引用")}</blockquote><div class="actions"><button id="source" ${item.source_document_id ? "" : "disabled"}>${esc(item.source_name || "无来源")} · 第 ${Number(item.source_page_start)} 页</button><button id="edit-evidence" ${disabled("edit")}>编辑</button><button id="annotations">标注</button><button id="delete-evidence" class="danger" ${disabled("edit")}>删除</button></div><h3>关联证据</h3>${relationHtml(state.relations.filter((row) => row.from_evidence_id === id || row.to_evidence_id === id))}`);
    bindEvidence(ctx); on("#source", "click", () => openPage(ctx, item.source_document_id, item.source_page_start)); on("#edit-evidence", "click", () => evidenceEdit(ctx, item)); on("#annotations", "click", () => annotations(ctx, id));
    on("#delete-evidence", "click", async (event) => { if (!confirm("删除该证据及关联标注、关系？此操作不可撤销。")) return; await writeAction(ctx, event.currentTarget, `/api/evidence/${id}`, { method: "DELETE" }, () => showView("evidence")); });
  }
  function evidenceEdit(ctx, item = {}) {
    const statuses = ["待复核", "已确认", "待质证", "待补证"].filter((status) => status !== "已确认" || allowed("approve"));
    sheet(item.id ? "编辑证据" : "添加证据", `<form id="evidence-form"><fieldset ${disabled("edit")}>${field("title", "证据标题", item.title, 'required minlength="2" maxlength="200"')}${select("category", "类别", choices(["书证", "物证", "言词证据", "银行流水", "审计报告", "询问笔录", "电子数据", "其他材料"]), item.category || "书证")}${select("credibility", "可信度", choices(["待核验", "高", "较高", "中", "低"]), item.credibility || "待核验")}${select("status", "状态", choices(statuses), item.status || "待复核")}${select("source_document_id", "来源文档", [...(item.source_document_id ? [] : [["", "无来源"]]), ...state.documents.map((doc) => [doc.id, doc.name])], item.source_document_id || "")}${field("source_page_start", "起始页", item.source_page_start || 1, 'type="number" min="1" required')}${field("source_page_end", "结束页", item.source_page_end || 1, 'type="number" min="1" required')}${area("fact", "待证事实", item.fact, "required")}${area("quote", "原文引用", item.quote)}<button type="submit" class="primary">保存证据</button></fieldset></form>`);
    bindForm("#evidence-form", ctx, "edit", (body) => {
      body.source_document_id = body.source_document_id ? Number(body.source_document_id) : null; body.source_page_start = Number(body.source_page_start); body.source_page_end = Number(body.source_page_end);
      if (body.source_page_end < body.source_page_start) throw new Error("结束页不能小于起始页");
      const doc = state.documents.find((row) => row.id === body.source_document_id); if (doc && body.source_page_end > doc.pages) throw new Error("页码超出来源文档范围");
      return api(item.id ? `/api/evidence/${item.id}` : casePath(ctx, "evidence"), json(body, item.id ? "PATCH" : "POST"));
    }, () => showView("evidence"));
  }
  async function annotations(ctx, evidenceId) {
    const seq = sheet("证据标注", empty("加载中…"));
    try {
      const rows = await api(`/api/evidence/${evidenceId}/annotations`); if (!sheetCurrent(ctx, seq)) return;
      $("#sheet-body").innerHTML = `${rows.map((row) => `<article class="item"><strong>${esc(row.annotation_type)} · ${esc(row.status)}</strong><p>${esc(row.content)}</p><small>${esc(row.user_name)} · ${esc(formatDate(row.created_at))}</small><div class="actions"><button data-edit-annotation="${Number(row.id)}" ${disabled("edit")}>编辑</button><button data-delete-annotation="${Number(row.id)}" ${disabled("edit")}>删除</button></div></article>`).join("") || empty()}<button id="new-annotation" ${disabled("edit")}>新增标注</button>`;
      const edit = (row = {}) => {
        sheet("编辑标注", `<form id="annotation-form"><fieldset ${disabled("edit")}>${select("annotation_type", "类型", choices(["备注", "疑点", "质证意见", "补证建议", "重点", "其他"]), row.annotation_type || "备注")}${area("content", "内容", row.content, 'required maxlength="10000"')}${select("status", "状态", choices(["待处理", "已处理"]), row.status || "待处理")}<button type="submit" class="primary">保存标注</button></fieldset></form>`);
        bindForm("#annotation-form", ctx, "edit", (body) => api(row.id ? `/api/evidence-annotations/${row.id}` : `/api/evidence/${evidenceId}/annotations`, json({ ...body, ...(!row.id ? { user_name: state.identity.name || "本机律师" } : {}) }, row.id ? "PATCH" : "POST")), () => annotations(ctx, evidenceId));
      };
      on("#new-annotation", "click", () => edit());
      document.querySelectorAll("[data-edit-annotation]").forEach((button) => button.onclick = () => edit(rows.find((row) => row.id === Number(button.dataset.editAnnotation))));
      document.querySelectorAll("[data-delete-annotation]").forEach((button) => button.onclick = () => { if (confirm("确定永久删除标注？")) writeAction(ctx, button, `/api/evidence-annotations/${Number(button.dataset.deleteAnnotation)}`, { method: "DELETE" }, () => annotations(ctx, evidenceId)); });
    } catch (error) { if (sheetCurrent(ctx, seq)) $("#sheet-body").innerHTML = `<p class="error">${esc(error.message)}</p>`; }
  }
  async function gaps(ctx) {
    const seq = sheet("疏漏检测结果", empty("加载中…"));
    try {
      const result = await api(casePath(ctx, "gap-analysis")); if (!sheetCurrent(ctx, seq)) return;
      $("#sheet-body").innerHTML = (result.gaps || []).map((gap) => `<article class="item"><span class="badge">${esc(gap.severity)} · ${esc(gap.gap_type)}</span><h3>${esc(gap.description)}</h3><p>${esc(gap.details || "")}</p><p>${esc(gap.suggestion || "")}</p></article>`).join("") || empty("暂无已记录疏漏");
    } catch (error) { if (sheetCurrent(ctx, seq)) $("#sheet-body").innerHTML = `<p class="error">${esc(error.message)}</p>`; }
  }
  async function writeAction(ctx, button, path, options, done, permission = "edit") {
    if (!current(ctx) || !allowed(permission) || button.disabled) return;
    button.disabled = true;
    try { const result = await api(path, options); if (visible(ctx)) await done(result); }
    catch (error) { fail(error, ctx); }
    finally { button.disabled = !allowed(permission); }
  }
  function caseAction(ctx, button, suffix, done) { return writeAction(ctx, button, casePath(ctx, suffix), { method: "POST" }, done); }
  async function chatView(ctx) {
    const conversations = await api(casePath(ctx, "conversations")); if (!visible(ctx)) return;
    $("#content").innerHTML = `<div class="heading"><h1>案件问答</h1><button id="chat-history">会话历史</button><button id="new-chat" ${disabled("edit")}>新建</button></div><p id="chat-title" class="muted">新会话</p><div id="messages">${empty("暂无消息")}</div><form id="chat-form" class="composer"><textarea name="question" id="question" aria-label="阅卷问题" placeholder="输入阅卷问题" required maxlength="3000" ${disabled("edit")}></textarea><div class="actions"><label class="check"><input type="checkbox" id="chat-llm">模型答复</label><button type="submit" class="primary" ${disabled("edit")}>发送</button></div><p id="chat-error" class="error" role="alert"></p></form>`;
    let rows = conversations, busy = false, archived = false;
    const renderMessages = (messages) => { $("#messages").innerHTML = messages.map((message) => `<article class="message ${message.role === "user" ? "user" : "assistant"}"><small>${message.role === "user" ? "提问" : "阅卷答复"}</small>${markdown(message.content)}${citations(message.citations)}</article>`).join("") || empty("暂无消息"); bindCitations(ctx, $("#messages")); };
    const load = async (id) => {
      if (busy || !visible(ctx)) return; state.conversationId = id; const seq = ++state.chatEpoch; closeSheet();
      $("#messages").innerHTML = empty("加载会话…");
      const row = rows.find((item) => item.id === id); archived = Boolean(row?.archived); $("#chat-title").textContent = row?.title || "新会话";
      $("#chat-form button").disabled = archived || !allowed("edit");
      try { const messages = id ? await api(`/api/conversations/${id}/messages`) : []; if (visible(ctx) && seq === state.chatEpoch) renderMessages(messages); }
      catch (error) { if (visible(ctx) && seq === state.chatEpoch) $("#messages").innerHTML = `<p class="error">${esc(error.message)}</p>`; }
    };
    on("#new-chat", "click", () => load(null));
    on("#chat-history", "click", async () => {
      const seq = sheet("会话历史", empty("加载中…"));
      try { rows = await api(casePath(ctx, "conversations")); if (!sheetCurrent(ctx, seq)) return;
        $("#sheet-body").innerHTML = rows.map((row) => `<button class="row" data-conversation="${Number(row.id)}"><strong>${esc(row.title)}</strong><small>${Number(row.message_count)} 条消息${row.archived ? " · 已归档" : ""}</small></button>`).join("") || empty();
        document.querySelectorAll("[data-conversation]").forEach((button) => button.onclick = () => load(Number(button.dataset.conversation)));
      } catch (error) { if (sheetCurrent(ctx, seq)) $("#sheet-body").innerHTML = `<p class="error">${esc(error.message)}</p>`; }
    });
    on("#chat-form", "submit", async (event) => {
      event.preventDefault(); if (busy || archived || !visible(ctx) || !allowed("edit")) return;
      const input = $("#question"), question = input.value.trim(); if (!question) return;
      busy = true; const seq = ++state.chatEpoch; const conversationId = state.conversationId;
      const button = $("#chat-form button"); button.disabled = true; $("#new-chat").disabled = true; $("#chat-history").disabled = true; $("#chat-error").textContent = "正在生成答复…";
      try {
        const result = await api(casePath(ctx, "chat"), json({ question, user_name: state.identity.name || "本机律师", conversation_id: conversationId, use_llm: $("#chat-llm").checked }));
        if (!visible(ctx) || seq !== state.chatEpoch) return;
        state.conversationId = result.conversation_id; input.value = ""; $("#chat-title").textContent = question.slice(0, 30);
        const messages = await api(`/api/conversations/${result.conversation_id}/messages`);
        if (!visible(ctx) || seq !== state.chatEpoch) return; renderMessages(messages); $("#chat-error").textContent = ""; $("#main").scrollTop = $("#main").scrollHeight;
      } catch (error) { if (visible(ctx) && seq === state.chatEpoch) $("#chat-error").textContent = `发送未确认：${error.message}。请检查会话历史后再决定重发。`; }
      finally { busy = false; if (visible(ctx)) { button.disabled = !allowed("edit"); $("#new-chat").disabled = !allowed("edit"); $("#chat-history").disabled = false; } }
    });
    if (state.conversationId) await load(state.conversationId);
  }
  function resultHtml(result) { return `<p class="muted">任务 #${Number(result.run_id || result.id)} · ${esc(result.status || result.runtime || result.agent_type || "完成")} · ${Number(result.total_ms || 0)}ms</p>${markdown(result.answer || "")}${citations(result.citations)}<h3>执行轨迹</h3>${(result.steps || []).map((step) => `<article class="item"><strong>${esc(step.role || step.agent_role || step.node_name)}</strong><small> · ${esc(step.status)} · ${Number(step.latency_ms || 0)}ms</small><p>${esc(step.summary || step.output?.summary || step.node || "")}</p></article>`).join("")}`; }
  async function tasks(ctx) {
    const metrics = await api(casePath(ctx, "platform-metrics")); if (!visible(ctx)) return;
    $("#content").innerHTML = `<h1>阅卷任务</h1><p class="muted">${Number(metrics.agent_runs?.total || 0)} 次任务 · 已索引 ${Number(metrics.vector_index?.pages || 0)} 页</p><form id="task-form">${area("question", "阅卷问题", "", 'required maxlength="3000"')}${select("mode", "执行方式", [["multi_agent", "标准分析"], ["langgraph", "可恢复分析"]], "multi_agent")}<label class="check"><input name="use_llm" type="checkbox">模型答复</label><button type="submit" class="primary" ${disabled("edit")}>运行任务</button></form><div id="task-result"></div><details><summary>高级：索引、评测与对比</summary><div class="actions"><button id="build-index" ${disabled("edit")}>更新索引</button><button id="evaluate" ${disabled("edit")}>检索评测</button><button id="compare" ${disabled("edit")}>分析方式对比</button></div><div id="advanced-result"></div></details><section class="section"><h2>最近任务</h2>${(metrics.recent_runs || []).map((run) => `<button class="row" data-run="${Number(run.id)}"><strong>${esc(run.question)}</strong><small>#${Number(run.id)} · ${esc(run.status)}${run.resumable ? " · 可恢复" : ""}</small></button>`).join("") || empty()}</section>`;
    const render = (result) => { if (visible(ctx)) { $("#task-result").innerHTML = resultHtml(result); bindCitations(ctx, $("#task-result")); } };
    const failure = (error) => { if (!visible(ctx)) return; $("#task-result").innerHTML = `<p class="error">${esc(error.message)}</p>${error.resumable && error.runId ? `<button id="resume" ${disabled("edit")}>恢复任务 #${Number(error.runId)}</button>` : ""}`; on("#resume", "click", (event) => resume(ctx, Number(error.runId), event.currentTarget, render, failure)); };
    const poll = async (jobId) => {
      while (visible(ctx)) {
        const job = await api(`/api/agent-jobs/${encodeURIComponent(jobId)}`); if (!visible(ctx)) return;
        if (Number(job.case_id) !== ctx.caseId) throw new Error("任务不属于当前案件");
        if (job.status === "completed") { sessionStorage.removeItem(`lexvault-mobile-job-${ctx.caseId}`); render(job.result); return; }
        if (["failed", "interrupted"].includes(job.status)) { sessionStorage.removeItem(`lexvault-mobile-job-${ctx.caseId}`); const error = new Error(job.error?.message || "任务中断"); error.runId = job.error?.run_id || job.run_id; error.resumable = Boolean(job.resumable || job.error?.resumable); throw error; }
        $("#task-result").innerHTML = `<p role="status">后台任务：${esc(job.status)}</p>${resultHtml({ ...job, answer: "" })}`;
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    };
    let busy = false;
    const run = async (jobId = null) => {
      if (busy || !visible(ctx) || !allowed("edit")) return; busy = true; const button = $("#task-form button"); button.disabled = true;
      try {
        if (!jobId) { const form = $("#task-form"); const body = Object.fromEntries(new FormData(form)); body.use_llm = form.elements.use_llm.checked; body.user_name = state.identity.name || "本机律师"; const submitted = await api(casePath(ctx, "agent-jobs"), json(body)); jobId = submitted.job_id; sessionStorage.setItem(`lexvault-mobile-job-${ctx.caseId}`, jobId); }
        await poll(jobId);
      } catch (error) { failure(error); }
      finally { busy = false; if (visible(ctx)) button.disabled = !allowed("edit"); }
    };
    on("#task-form", "submit", (event) => { event.preventDefault(); run(); });
    document.querySelectorAll("[data-run]").forEach((button) => button.onclick = async () => {
      const seq = sheet("任务详情", empty("加载中…"));
      try { const result = await api(`/api/agent-runs/${Number(button.dataset.run)}`); if (!sheetCurrent(ctx, seq)) return; $("#sheet-body").innerHTML = resultHtml(result) + (result.resumable ? `<button id="resume-detail" ${disabled("edit")}>恢复任务</button>` : ""); bindCitations(ctx, $("#sheet-body")); on("#resume-detail", "click", (event) => { closeSheet(); resume(ctx, Number(button.dataset.run), event.currentTarget, render, failure); }); }
      catch (error) { if (sheetCurrent(ctx, seq)) $("#sheet-body").innerHTML = `<p class="error">${esc(error.message)}</p>`; }
    });
    on("#build-index", "click", (event) => caseAction(ctx, event.currentTarget, "vector-index", (result) => { $("#advanced-result").innerHTML = `<p>已索引 ${Number(result.indexed?.pages || 0)} 页</p>`; }));
    on("#evaluate", "click", (event) => caseAction(ctx, event.currentTarget, "evaluate-rag", (report) => { $("#advanced-result").innerHTML = `<h3>检索质量</h3><p>召回率 ${report.recall_at_k == null ? "未提供" : `${Math.round(report.recall_at_k * 100)}%`} · 排序得分 ${esc(report.mrr ?? "未提供")} · ${Number(report.queries)} 题</p>${(report.cases || []).map((item) => `<article class="item"><p>${esc(item.query)}</p><small>召回率 ${esc(item.recall_at_k ?? "无标准答案")}</small></article>`).join("")}`; }));
    on("#compare", "click", (event) => {
      const form = $("#task-form"); if (!form.reportValidity()) return;
      writeAction(ctx, event.currentTarget, casePath(ctx, "agent-compare"), json({ question: form.elements.question.value, mode: form.elements.mode.value, use_llm: form.elements.use_llm.checked, user_name: state.identity.name || "本机律师" }), (result) => { $("#advanced-result").innerHTML = `<h3>分析方式对比</h3><p>结构${result.comparison.structurally_equivalent ? "一致" : "存在差异"} · 引用${result.comparison.citation_match ? "一致" : "存在差异"}</p><h3>标准分析</h3>${resultHtml(result.native)}<h3>可恢复分析</h3>${resultHtml(result.langgraph)}`; bindCitations(ctx, $("#advanced-result")); });
    });
    const active = sessionStorage.getItem(`lexvault-mobile-job-${ctx.caseId}`); if (active) await run(active);
  }
  async function resume(ctx, runId, button, render, failure) {
    if (!current(ctx) || !allowed("edit") || button.disabled) return; button.disabled = true;
    try { const result = await api(`/api/agent-runs/${runId}/resume`, { method: "POST" }); if (visible(ctx)) render(result); }
    catch (error) { if (visible(ctx)) failure(error); } finally { button.disabled = !allowed("edit"); }
  }
  async function exportsView(ctx) {
    const templates = await api("/api/export-templates"); if (!visible(ctx)) return;
    $("#content").innerHTML = `<h1>成果导出</h1>${select("template", "结案模板", [["", "默认完整归档"], ...templates.map((item) => [item.id, item.name])], "")}<p id="template-description" class="muted"></p><div class="section"><h2>审阅包</h2><ul><li>案件摘要</li><li>内容级目录</li><li>证据目录</li><li>问答记录</li><li>原始卷宗</li></ul><div class="actions"><button id="export" class="primary" ${disabled("export")}>下载 ZIP</button><button id="export-final" ${disabled("export")}>结案打包</button></div></div>`;
    on('[name="template"]', "change", (event) => { $("#template-description").textContent = templates.find((item) => String(item.id) === event.target.value)?.description || ""; });
    const start = async (button, final) => {
      if (!allowed("export") || !current(ctx)) return;
      const params = new URLSearchParams(); const id = $('[name="template"]').value; if (id) params.set("template_id", id); if (final) params.set("final", "true");
      button.disabled = true; try { await download(ctx, `${casePath(ctx, "export")}?${params}`, `案件-${ctx.caseId}.zip`); } finally { button.disabled = !allowed("export"); }
    };
    on("#export", "click", (event) => start(event.currentTarget, false)); on("#export-final", "click", (event) => start(event.currentTarget, true));
  }
  async function download(ctx, path, filename) {
    try {
      const response = await api(path); const blob = await response.blob(); if (!current(ctx)) return;
      const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      notice(response.headers.get("x-package-sha256") ? `审阅包已生成，SHA256：${response.headers.get("x-package-sha256")}` : "文件已准备下载");
    } catch (error) { fail(error, ctx); }
  }
  function caseStorage(ctx, status) {
    const seq = sheet(status === "archived" ? "已归档案件" : "回收站", empty("加载中…"));
    api(`/api/cases/lifecycle/${status}`).then((rows) => {
      if (!sheetCurrent(ctx, seq)) return;
      $("#sheet-body").innerHTML = rows.map((row) => `<article class="item"><strong>${esc(row.title)}</strong><p class="muted">${status === "trash" ? `保留至 ${esc(row.purge_after || "")}` : "长期归档"}</p><button data-restore="${Number(row.id)}" ${disabled("edit")}>恢复案件</button></article>`).join("") || empty();
      document.querySelectorAll("[data-restore]").forEach((button) => button.onclick = async () => {
        if (!allowed("edit") || !sheetCurrent(ctx, seq)) return; button.disabled = true;
        try { await api(`/api/cases/${Number(button.dataset.restore)}/restore`, { method: "POST" }); if (sheetCurrent(ctx, seq)) { state.caseId = Number(button.dataset.restore); await checkIdentity(); } }
        catch (error) { if (sheetCurrent(ctx, seq)) $("#sheet-error").textContent = error.message; button.disabled = false; }
      });
    }).catch((error) => { if (sheetCurrent(ctx, seq)) $("#sheet-error").textContent = error.message; });
  }
  on("#case-select", "change", (event) => chooseCase(event.target.value));
  document.querySelectorAll("[data-view]").forEach((button) => button.onclick = () => showView(button.dataset.view));
  on("#sheet-close", "click", closeSheet); on("#sheet", "cancel", () => { state.sheetEpoch += 1; sheetHistory.length = 0; });
  on("#more", "click", () => {
    sheet("更多", `<div class="menu"><button id="new-case" ${disabled("manage")}>新建案件</button><button id="archive-case" ${disabled("edit")}>归档当前案件</button><button id="trash-case" class="danger" ${disabled("edit")}>当前案件移入回收站</button><button id="archived-cases">已归档案件</button><button id="trash-cases">回收站</button><button id="open-tasks">阅卷任务</button><button id="open-export">成果导出</button><button id="account">账号</button><a class="button" href="/?ui=desktop">桌面工作台</a></div>`);
    on("#new-case", "click", () => {
      const ctx = context(); sheet("新建案件", `<form id="case-form"><fieldset ${disabled("manage")}>${field("title", "案件名称", "", 'required minlength="2" maxlength="120"')}${field("case_no", "案号")}${select("case_type", "案件类型", choices(["刑事", "金融犯罪", "民商事", "行政"]), "刑事")}${field("client_name", "当事人")}${area("description", "案件说明")}<button type="submit" class="primary">创建案件</button></fieldset></form>`);
      bindForm("#case-form", ctx, "manage", (body) => api("/api/cases", json(body)), async (created) => { state.caseId = created.id; await checkIdentity(); });
    });
    const lifecycle = async (action, prompt) => {
      const ctx = context(); if (!current(ctx) || !allowed("edit") || !confirm(prompt)) return;
      try { await api(`/api/cases/${ctx.caseId}/${action}`, { method: "POST" }); if (current(ctx)) { state.caseId = null; await checkIdentity(); } } catch (error) { if (current(ctx)) $("#sheet-error").textContent = error.message; }
    };
    on("#archive-case", "click", () => lifecycle("archive", "归档当前案件？归档后可随时恢复。")); on("#trash-case", "click", () => lifecycle("trash", "将当前案件移入回收站并保留 30 天？"));
    on("#archived-cases", "click", () => caseStorage(context(), "archived")); on("#trash-cases", "click", () => caseStorage(context(), "trash"));
    on("#open-tasks", "click", () => showView("tasks")); on("#open-export", "click", () => showView("export"));
    on("#account", "click", () => {
      const identity = state.identity; sheet("账号", `<h3>${esc(identity.name || "本机律师")}</h3><p>${esc(identity.organization_name || "本地工作空间")}</p><p>${identity.admin ? "管理员" : esc((identity.permissions || []).join(" · "))}</p><p>${esc(identity.support_contact || "")}</p>${identity.mode === "token" ? '<button id="logout">退出登录</button>' : empty("本机模式")}`);
      on("#logout", "click", async (event) => { event.currentTarget.disabled = true; try { await api("/api/auth/session", { method: "DELETE" }); expire(); } catch (error) { $("#sheet-error").textContent = error.message; event.currentTarget.disabled = false; } });
    });
  });
  on("#auth-retry", "click", () => checkIdentity());
  on("#reveal-token", "click", () => {
    const input = $('#login [name="token"]'), button = $("#reveal-token"); const reveal = input.type === "password";
    input.type = reveal ? "text" : "password"; button.textContent = reveal ? "隐藏" : "显示"; button.setAttribute("aria-pressed", String(reveal));
  });
  on("#login", "submit", async (event) => {
    event.preventDefault(); const form = event.currentTarget, button = form.querySelector('[type="submit"]'); if (button.disabled) return; button.disabled = true;
    const token = form.elements.token.value; form.elements.token.value = "";
    try { await api("/api/auth/session", json({ token })); $("#auth-error").textContent = ""; await checkIdentity(); }
    catch (error) { $("#auth-error").textContent = error.message; }
    finally { button.disabled = false; }
  });
  const viewport = () => {
    const vv = window.visualViewport; if (!vv) return;
    document.documentElement.style.setProperty("--viewport", `${vv.height}px`); document.documentElement.style.setProperty("--top", `${vv.offsetTop}px`);
    document.body.classList.toggle("keyboard", window.innerHeight - vv.height > 140 && /INPUT|TEXTAREA/.test(document.activeElement?.tagName || ""));
  };
  window.visualViewport?.addEventListener("resize", viewport); window.visualViewport?.addEventListener("scroll", viewport); window.addEventListener("focusout", viewport); viewport();
  checkIdentity();
})();
