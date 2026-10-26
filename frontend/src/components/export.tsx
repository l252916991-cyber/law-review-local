"use client";
import { useState } from "react";
import { api, jsonBody } from "@/lib/api";
import { id, message, object, text, type Row } from "@/lib/data";
import { ErrorBox, list, useData } from "./ui";

export function ExportCase({ caseId, identity }: { caseId: number; identity: Row }) {
  const templates = useData("/api/export-templates");
  const reports = useData(`/api/cases/${caseId}/reports`);
  const [template, setTemplate] = useState(""),
    [final, setFinal] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [name, setName] = useState(""),
    [blocks, setBlocks] = useState(
      '[{"type":"case_summary","title":"案件摘要"},{"type":"catalog_csv","title":"卷宗目录"}]',
    );
  const role = text((identity.roles ? object(identity.roles) : {})[String(caseId)]),
    permissions = Array.isArray(identity.permissions) ? identity.permissions.map(String) : [],
    canEdit = Boolean(identity.admin) || (permissions.includes("edit") && !["reviewer", "viewer"].includes(role)),
    canReview = Boolean(identity.admin) || (permissions.includes("approve") && ["owner", "reviewer", ""].includes(role)),
    reportData = reports.data ? object(reports.data) : {},
    items = list(reportData.reports),
    basis = reportData.current_basis ? object(reportData.current_basis) : {},
    counts = basis.counts ? object(basis.counts) : {},
    deliverable = Boolean(reportData.deliverable);

  async function createReport() {
    setBusy(true);
    setError("");
    try {
      const result = object(await api(`/api/cases/${caseId}/reports`, jsonBody({})));
      setNotice(`已创建 ${text(object(result.report).title)}`);
      reports.reload();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  async function transition(report: Row, action: string, extra: Record<string, unknown> = {}) {
    setBusy(true);
    setError("");
    try {
      await api(`/api/reports/${id(report.id)}`, jsonBody({ action, ...extra }, "PATCH"));
      setNotice(`报告 v${text(report.version)} 已${actionLabel(action)}`);
      reports.reload();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  function returnReport(report: Row) {
    const reason = window.prompt("退回原因（必填）：", "");
    if (reason === null) return;
    if (!reason.trim()) {
      setError("退回报告必须填写原因");
      return;
    }
    void transition(report, "return", { note: reason.trim() });
  }

  return (
    <section>
      <header>
        <span className="eyebrow">成果导出</span>
        <h2>生成案件交付包</h2>
      </header>
      <ErrorBox error={error || templates.error || reports.error} />
      {notice ? <p className="audit-note">{notice}</p> : null}

      <section className="card">
        <header>
          <h2>报告复核与签发</h2>
          <span className="badge">{deliverable ? "可正式交付" : "需批准报告"}</span>
        </header>
        <p className="audit-note">
          当前依据：材料 {text(counts.documents) || "0"} 份 / {text(counts.pages) || "0"} 页，证据{" "}
          {text(counts.evidence) || "0"} 条（已确认 {text(counts.confirmed) || "0"}）。批准后若材料或证据再变，
          该批准自动失效，需要重新复核。
        </p>
        {canEdit && (
          <button className="primary" disabled={busy} onClick={() => void createReport()}>
            新建报告草稿
          </button>
        )}
        {items.map((report) => {
          const status = text(report.status);
          return (
            <article className="row" key={id(report.id)}>
              <span>
                <strong>
                  v{text(report.version)} {text(report.title)}
                </strong>
                <small>
                  编制：{text(report.prepared_by) || "—"} · 复核：{text(report.reviewed_by) || "—"} · 批准：
                  {text(report.approved_by) || "—"}
                  {text(report.return_reason) ? ` · 退回原因：${text(report.return_reason)}` : ""}
                </small>
              </span>
              <span>
                {report.stale ? <span className="badge">依据已变化</span> : null}
                <span className="badge">{status}</span>
                {canEdit && ["草稿", "已退回"].includes(status) && (
                  <button disabled={busy} onClick={() => void transition(report, "submit")}>
                    提交复核
                  </button>
                )}
                {canReview && status === "待复核" && (
                  <>
                    <button disabled={busy} onClick={() => void transition(report, "approve")}>
                      批准
                    </button>
                    <button disabled={busy} onClick={() => returnReport(report)}>
                      退回
                    </button>
                  </>
                )}
                {canReview && status !== "已作废" && (
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm("确认作废该报告？作废后不能再正式导出。"))
                        void transition(report, "void", { note: "操作人作废" });
                    }}
                  >
                    作废
                  </button>
                )}
              </span>
            </article>
          );
        })}
        {!items.length && (
          <p className="audit-note">还没有报告。承办律师创建草稿并提交复核，复核律师批准后即可正式交付。</p>
        )}
      </section>

      <section className="card">
        <label>
          导出模板
          <select value={template} onChange={(e) => setTemplate(e.target.value)}>
            <option value="">默认模板</option>
            {list(templates.data).map((row) => (
              <option value={id(row.id)} key={id(row.id)}>
                {text(row.name)}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          <input type="checkbox" checked={final} onChange={(e) => setFinal(e.target.checked)} />
          正式交付（要求已批准且依据未变化的报告）
        </label>
        {final && !deliverable ? (
          <p role="status" className="audit-note">
            当前没有可交付的报告：请先在上一节提交并批准一份报告；依据变化时需要重新复核。
          </p>
        ) : null}
        <p>包内包含所选模板要求的目录、证据、问答和附件，导出会记录审计。</p>
        <a
          className="button primary"
          href={`/api/cases/${caseId}/export?final=${final}${template ? `&template_id=${Number(template)}` : ""}`}
        >
          下载案件交付包
        </a>
      </section>

      <details className="card">
        <summary>管理导出模板</summary>
        <p>需要管理权限，内置模板不可修改或删除。</p>
        {list(templates.data).map((row) => (
          <div className="row" key={id(row.id)}>
            <span>
              {text(row.name)}
              <small>{text(row.description)}</small>
            </span>
            {!row.builtin ? (
              <button
                onClick={async () => {
                  if (!window.confirm("确认删除此模板？")) return;
                  try {
                    await api(`/api/export-templates/${id(row.id)}`, { method: "DELETE" });
                    templates.reload();
                  } catch (e) {
                    setError(message(e));
                  }
                }}
              >
                删除模板
              </button>
            ) : null}
          </div>
        ))}
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await api("/api/export-templates", jsonBody({ name, blocks: JSON.parse(blocks) }));
              templates.reload();
              setName("");
              setError("");
            } catch (e) {
              setError(message(e));
            }
          }}
        >
          <label>
            模板名称
            <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={60} />
          </label>
          <label>
            模板区块 JSON
            <textarea value={blocks} onChange={(e) => setBlocks(e.target.value)} required />
          </label>
          <button>创建模板</button>
        </form>
      </details>
    </section>
  );
}

const ACTION_LABELS: Record<string, string> = { submit: "提交复核", approve: "批准", return: "退回", void: "作废" };

function actionLabel(action: string) {
  return ACTION_LABELS[action] ?? action;
}
