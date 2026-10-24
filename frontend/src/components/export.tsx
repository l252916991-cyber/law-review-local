"use client";
import { useState } from "react";
import { api, jsonBody } from "@/lib/api";
import { id, message, text } from "@/lib/data";
import { ErrorBox, list, useData } from "./ui";

export function ExportCase({ caseId }: { caseId: number }) {
  const templates = useData("/api/export-templates");
  const [template, setTemplate] = useState(""),
    [final, setFinal] = useState(false),
    [error, setError] = useState(""),
    [name, setName] = useState(""),
    [blocks, setBlocks] = useState(
      '[{"type":"case_summary","title":"案件摘要"},{"type":"catalog_csv","title":"卷宗目录"}]',
    );
  return (
    <section>
      <header>
        <span className="eyebrow">成果导出</span>
        <h2>生成案件交付包</h2>
      </header>
      <ErrorBox error={error || templates.error} />
      <section className="card">
        <label>
          导出模板
          <select
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
          >
            <option value="">默认模板</option>
            {list(templates.data).map((row) => (
              <option value={id(row.id)} key={id(row.id)}>
                {text(row.name)}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          <input
            type="checkbox"
            checked={final}
            onChange={(e) => setFinal(e.target.checked)}
          />
          正式交付（服务端检查材料及审批状态）
        </label>
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
                    await api(`/api/export-templates/${id(row.id)}`, {
                      method: "DELETE",
                    });
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
              await api(
                "/api/export-templates",
                jsonBody({ name, blocks: JSON.parse(blocks) }),
              );
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
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={60}
            />
          </label>
          <label>
            模板区块 JSON
            <textarea
              value={blocks}
              onChange={(e) => setBlocks(e.target.value)}
              required
            />
          </label>
          <button>创建模板</button>
        </form>
      </details>
    </section>
  );
}
