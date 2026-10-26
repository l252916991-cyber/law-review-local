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
    [preview, setPreview] = useState<Row | null>(null),
    [name, setName] = useState(""),
    [blocks, setBlocks] = useState(
      '[{"type":"case_summary","title":"案件摘要"},{"type":"catalog_csv","title":"卷宗目录"}]',
    );
  const role = text((identity.roles ? object(identity.roles) : {})[String(caseId)]),
    permissions = Array.isArray(identity.permissions) ? identity.permissions.map(String) : [],
    canEdit = Boolean(identity.admin) || (permissions.includes("edit") && !["reviewer", "viewer"].includes(role)),
    // A case role only narrows a member's permissions: owner/reviewer never
    // restore a withdrawn approve right, and an environment identity without a
    // case role cannot review.
    canReview = Boolean(identity.admin) || (permissions.includes("approve") && ["owner", "reviewer"].includes(role)),
    reportData = reports.data ? object(reports.data) : {},
    items = list(reportData.reports),
    basis = reportData.current_basis ? object(reportData.current_basis) : {},
    counts = basis.counts ? object(basis.counts) : {},
    deliverable = Boolean(reportData.deliverable),
    deliverableId = Number(reportData.deliverable_report_id) || 0;

  async function createReport() {
    setBusy(true);
    setError("");
    try {
      const result = object(
        await api(
          `/api/cases/${caseId}/reports`,
          jsonBody(template ? { template_id: Number(template) } : {}),
        ),
      );
      setNotice(`已创建 ${text(object(result.report).title)}，冻结模板：${templateName(template)}`);
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

  async function openPreview(report: Row) {
    setBusy(true);
    setError("");
    try {
      setPreview(object(await api(`/api/reports/${id(report.id)}/frozen`)));
    } catch (e) {
      setPreview(null);
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

  function templateName(value: string) {
    if (!value) return "默认结案包";
    const match = list(templates.data).find((row) => id(row.id) === Number(value));
    return match ? text(match.name) : `模板 #${value}`;
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
        <label>
          结案模板
          <select value={template} onChange={(e) => setTemplate(e.target.value)}>
            <option value="">默认结案包（不校验必选区块）</option>
            {list(templates.data).map((row) => (
              <option value={id(row.id)} key={id(row.id)}>
                {text(row.name)}
              </option>
            ))}
          </select>
        </label>
        <p className="audit-note">
          提交复核时冻结当前正文（所选模板、案件摘要、目录、证据、问答与全量流水），批准只覆盖该冻结版本。
          当前依据：材料 {text(counts.documents) || "0"} 份 / {text(counts.pages) || "0"} 页，证据{" "}
          {text(counts.evidence) || "0"} 条（已确认 {text(counts.confirmed) || "0"}）。
        </p>
        {canEdit && (
          <button className="primary" disabled={busy} onClick={() => void createReport()}>
            新建报告草稿（{templateName(template)}）
          </button>
        )}
        {items.map((report) => {
          const status = text(report.status);
          const canReviewReport = typeof report.can_review === "boolean" ? report.can_review : canReview;
          const frozen = Number(report.frozen_version_id) || 0;
          return (
            <article className="row" key={id(report.id)}>
              <span>
                <strong>
                  v{text(report.version)} {text(report.title)}
                </strong>
                <small>
                  模板：{report.template_id ? `#${text(report.template_id)}` : "默认结案包"}
                  {frozen ? ` · 冻结版本 #${frozen}` : ""}
                  {report.content_hash ? ` · 正文摘要 ${text(report.content_hash).slice(0, 12)}` : ""}
                </small>
                <small>
                  编制：{text(report.prepared_by) || "—"} · 复核：{text(report.reviewed_by) || "—"} · 批准：
                  {text(report.approved_by) || "—"}
                  {text(report.return_reason) ? ` · 退回原因：${text(report.return_reason)}` : ""}
                </small>
              </span>
              <span>
                {report.stale ? <span className="badge">依据已变化</span> : null}
                <span className="badge">{status}</span>
                {frozen ? (
                  <button disabled={busy} onClick={() => void openPreview(report)}>
                    冻结预览
                  </button>
                ) : null}
                {canEdit && ["草稿", "已退回"].includes(status) && (
                  <button disabled={busy} onClick={() => void transition(report, "submit")}>
                    提交复核
                  </button>
                )}
                {canReviewReport && status === "待复核" && (
                  <>
                    <button disabled={busy} onClick={() => void transition(report, "approve")}>
                      批准
                    </button>
                    <button disabled={busy} onClick={() => returnReport(report)}>
                      退回
                    </button>
                  </>
                )}
                {canReviewReport && status !== "已作废" && (
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
          <p className="audit-note">还没有报告。选择模板创建草稿，提交复核后被冻结的正文才会成为交付内容。</p>
        )}
      </section>

      {preview ? <FrozenPreview data={preview} onClose={() => setPreview(null)} /> : null}

      <section className="card">
        <label className="inline">
          <input type="checkbox" checked={final} onChange={(e) => setFinal(e.target.checked)} />
          正式交付（使用已批准报告的冻结正文与冻结模板）
        </label>
        {final && !deliverable ? (
          <p role="status" className="audit-note">
            当前没有可交付的报告：请先提交并批准一份报告；依据变化时需要重新准备、冻结并复核。
          </p>
        ) : null}
        <p>
          正式交付只读取批准版本的冻结正文：正文摘要、来源摘要与文件摘要都登记在包内清单，模板无法被替换。
          预览包不签发，仅供内部核对。
        </p>
        {final ? (
          <a className="button primary" href={`/api/cases/${caseId}/export?final=true&report_id=${deliverableId}`}>
            下载已批准版本
          </a>
        ) : (
          <a
            className="button primary"
            href={`/api/cases/${caseId}/export${template ? `?template_id=${Number(template)}` : ""}`}
          >
            下载预览包
          </a>
        )}
      </section>

      <details className="card">
        <summary>管理导出模板</summary>
        <p>
          模板的区块与静态正文都进入冻结正文；改动模板会让已批准报告的依据失效，需要重新准备并复核。
          内置模板不可修改或删除。
        </p>
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

function FrozenPreview({ data, onClose }: { data: Row; onClose: () => void }) {
  const report = data.report ? object(data.report) : {},
    blocks = list(data.blocks),
    transactions = data.transactions ? object(data.transactions) : {},
    currencies = Array.isArray(transactions.currencies) ? transactions.currencies.map(String) : [],
    attachments = list(data.attachments);
  return (
    <section className="card">
      <header>
        <h2>冻结正文预览</h2>
        <button onClick={onClose}>关闭</button>
      </header>
      <p className="audit-note">
        报告 v{text(report.version)} · 冻结版本 #{text(report.frozen_seq)} · 正文摘要{" "}
        {text(report.content_hash).slice(0, 16)} · 来源摘要 {text(report.source_hash).slice(0, 16)} · 冻结人{" "}
        {text(report.frozen_by) || "—"} · {text(report.frozen_at)}
      </p>
      <p className="audit-note">
        资金流水 {text(transactions.count) || "0"} 行（币种：{currencies.join("、") || "—"}），附件{" "}
        {attachments.length} 份。批准与正式交付都读取以下同一版本内容。
      </p>
      {blocks.map((block) => (
        <article key={text(block.archive_name)}>
          <h3>
            {text(block.title)} · {text(block.archive_name)}
          </h3>
          <pre>{text(block.text)}</pre>
          {block.truncated ? (
            <p className="audit-note">内容过长，仅显示前 200000 字符；下载包内为完整文件。</p>
          ) : null}
        </article>
      ))}
      {attachments.length ? (
        <ul>
          {attachments.map((item) => (
            <li key={text(item.archive_name)}>
              {text(item.archive_name)} · {text(item.size)} 字节 · {text(item.sha256).slice(0, 12)}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

const ACTION_LABELS: Record<string, string> = { submit: "提交复核", approve: "批准", return: "退回", void: "作废" };

function actionLabel(action: string) {
  return ACTION_LABELS[action] ?? action;
}
