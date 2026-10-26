"use client";
import { useEffect, useState } from "react";
import { ApiError, api, jsonBody } from "@/lib/api";
import { id, object, text, message, type Row } from "@/lib/data";
import { ErrorBox, Fields, Modal, Empty, list, useData, values } from "./ui";
import type { PageTarget } from "./preview";

export function Documents({
  caseId,
  preview,
}: {
  caseId: number;
  preview: (target: PageTarget) => void;
}) {
  const { data, error, reload } = useData(`/api/cases/${caseId}/documents`);
  const [query, setQuery] = useState(""),
    [edit, setEdit] = useState<Row | null>(null),
    [failure, setFailure] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const documents = list(data).filter((row) =>
    ["name", "people", "date_range", "doc_type", "summary"].some((key) =>
      text(row[key]).includes(query),
    ),
  );
  return (
    <section>
      <header className="section-header">
        <div>
          <span className="eyebrow">卷宗管理</span>
          <h2>双层卷宗目录</h2>
          <p>整理材料，校准目录，回到每一页原文。</p>
        </div>
        <label className="button primary">
          {busy ? "正在导入…" : "＋ 上传卷宗"}
          <input
            aria-label="上传卷宗"
            type="file"
            multiple
            hidden
            disabled={busy}
            onChange={async (event) => {
              const files = event.target.files;
              if (!files?.length) return;
              const body = new FormData();
              Array.from(files).forEach((file) => body.append("files", file));
              setBusy(true);
              setFailure("");
              setNotice("");
              try {
                const result = object(
                  await api(`/api/cases/${caseId}/documents`, {
                    method: "POST",
                    body,
                  }),
                );
                setNotice(`导入完成：${list(result.documents).length} 个文件`);
                setFailure(
                  list(result.failures)
                    .map((row) => `${text(row.name)}：${text(row.error)}`)
                    .join("；"),
                );
                reload();
              } catch (e) {
                setFailure(message(e));
              } finally {
                setBusy(false);
              }
            }}
          />
        </label>
      </header>
      <ErrorBox error={error || failure} />
      <p role="status">{notice}</p>
      <input
        className="search"
        aria-label="搜索卷宗"
        placeholder="搜索文件、人员、日期或摘要"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>文件 / 内容</th>
              <th>类型</th>
              <th>人员 / 日期</th>
              <th>页数</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((row) => (
              <tr key={id(row.id)}>
                <td>
                  <button
                    className="text-button"
                    onClick={() => preview({ document: id(row.id), page: 1 })}
                  >
                    {text(row.name)}
                  </button>
                  <small>{text(row.summary)}</small>
                </td>
                <td>{text(row.doc_type)}</td>
                <td>
                  {text(row.people)}
                  <small>{text(row.date_range)}</small>
                </td>
                <td>{text(row.pages)}</td>
                <td>
                  <span className="badge">{text(row.status)}</span>
                </td>
                <td>
                  <button onClick={() => setEdit(row)}>校准目录</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!documents.length && (
          <Empty>
            {data ? "暂无匹配卷宗，上传材料后开始阅卷。" : "正在加载卷宗…"}
          </Empty>
        )}
      </div>
      <BatchImport caseId={caseId} completed={reload} />
      {edit && (
        <Modal title="校准目录" close={() => setEdit(null)}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                const changes = values(e.currentTarget);
                await api(
                  `/api/documents/${id(edit.id)}/directory`,
                  jsonBody(
                    {
                      ...changes,
                      ...(edit.version
                        ? { expected_version: Number(edit.version) }
                        : {}),
                    },
                    "PATCH",
                  ),
                );
                setEdit(null);
                reload();
              } catch (err) {
                setFailure(message(err));
                if (err instanceof ApiError && err.status === 409) {
                  // 同事已先保存：关闭表单并刷新到最新目录。
                  setEdit(null);
                  reload();
                }
              }
            }}
          >
            <Fields
              names={{
                doc_type: "文书类型",
                people: "涉及人员",
                date_range: "时间范围",
                summary: "摘要",
                status: "状态",
              }}
              values={edit}
            />
            <button className="primary">保存校准</button>
            <ErrorBox error={failure} />
          </form>
        </Modal>
      )}
    </section>
  );
}

function BatchImport({
  caseId,
  completed,
}: {
  caseId: number;
  completed: () => void;
}) {
  const [batchId, setBatchId] = useState<number | null>(null),
    [progress, setProgress] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (batchId === null) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = object(
          await api(`/api/batch-imports/${batchId}`, {
            signal: controller.signal,
          }),
        );
        if (controller.signal.aborted) return;
        setProgress(result);
        if (["completed", "failed", "partial"].includes(text(result.status)))
          completed();
        else timer = setTimeout(() => void poll(), 1500);
      } catch (e) {
        if (!controller.signal.aborted) setError(message(e));
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [batchId, completed]);
  return (
    <details className="card">
      <summary>批量导入（需队列服务）</summary>
      <p>适用于大量卷宗。队列不可用时请使用上方直接上传。</p>
      <label className="button">
        {busy ? "正在提交…" : "选择批量文件"}
        <input
          type="file"
          multiple
          hidden
          disabled={busy}
          aria-label="批量文件"
          onChange={async (e) => {
            if (!e.target.files?.length) return;
            const body = new FormData();
            Array.from(e.target.files).forEach((file) =>
              body.append("files", file),
            );
            setBusy(true);
            setError("");
            setProgress(null);
            try {
              const result = object(
                await api(`/api/cases/${caseId}/batch-import`, {
                  method: "POST",
                  body,
                }),
              );
              setBatchId(id(result.batch_id));
            } catch (e) {
              setError(message(e));
            } finally {
              setBusy(false);
            }
          }}
        />
      </label>
      <ErrorBox error={error} />
      {progress && (
        <div role="status">
          <p>
            {text(progress.status)} · {text(progress.processed_files)}/
            {text(progress.total_files)} 个文件
          </p>
          <progress max={100} value={Number(progress.progress_percent) || 0} />
          {Array.isArray(progress.error_log) &&
            progress.error_log.map((line, index) => (
              <p className="error" key={index}>
                {text(line)}
              </p>
            ))}
        </div>
      )}
    </details>
  );
}
