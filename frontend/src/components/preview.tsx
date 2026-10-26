"use client";
import { useState, type FormEvent } from "react";
import { ApiError, api, jsonBody } from "@/lib/api";
import { message, object, text } from "@/lib/data";
import { ErrorBox, useData } from "./ui";

export type PageTarget = { document: number; page: number };
export function Preview({
  target,
  close,
  select,
}: {
  target: PageTarget;
  close: () => void;
  select: (target: PageTarget) => void;
}) {
  const { data, error, reload } = useData(
    `/api/documents/${target.document}/pages/${target.page}`,
  );
  const page = data ? object(data) : {};
  const [editing, setEditing] = useState(false),
    [failure, setFailure] = useState(""),
    [notice, setNotice] = useState("");

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const expected = Number(page.document_version);
    try {
      const result = object(
        await api(
          `/api/documents/${target.document}/pages/${target.page}`,
          jsonBody(
            {
              text: String(form.get("text") ?? ""),
              reason: String(form.get("reason") ?? ""),
              ...(Number.isSafeInteger(expected)
                ? { expected_version: expected }
                : {}),
            },
            "PATCH",
          ),
        ),
      );
      const invalidated = Array.isArray(result.invalidated)
        ? result.invalidated.length
        : 0;
      setEditing(false);
      setFailure("");
      setNotice(
        invalidated
          ? `原文已保存；${invalidated} 条引用不再匹配的已确认证据已退回待复核。`
          : "原文已保存。",
      );
      reload();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // 同事先保存过：刷新当前版本，不覆盖对方内容。
        setEditing(false);
        reload();
      }
      setFailure(message(err));
    }
  }

  return (
    <aside className="preview" aria-label="卷宗原文">
      <header>
        <div>
          <span className="eyebrow">原页溯源</span>
          <h2>{text(page.name) || "卷宗原文"}</h2>
        </div>
        <button onClick={close} aria-label="关闭原文">
          ×
        </button>
      </header>
      <nav>
        <button
          disabled={target.page <= 1}
          onClick={() => select({ ...target, page: target.page - 1 })}
        >
          上一页
        </button>
        <span>第 {target.page} 页</span>
        <button onClick={() => select({ ...target, page: target.page + 1 })}>
          下一页
        </button>
        <button onClick={() => setEditing((value) => !value)}>
          {editing ? "取消纠错" : "纠错原文"}
        </button>
      </nav>
      <ErrorBox error={error || failure} />
      {notice ? <p className="audit-note">{notice}</p> : null}
      {editing ? (
        <form className="preview-edit" onSubmit={save}>
          <label>
            纠正后的原文
            <textarea
              name="text"
              required
              defaultValue={text(page.text)}
              rows={16}
            />
          </label>
          <label>
            纠错原因（可选，会写入审计）
            <input name="reason" maxLength={300} />
          </label>
          <p>
            保存后，来源为该页且引用不再匹配的“已确认”证据会退回“待复核”，
            需要重新确认。
          </p>
          <button className="primary">保存原文</button>
        </form>
      ) : (
        <pre>{data ? text(page.text) : error ? "" : "正在加载原文…"}</pre>
      )}
      <a
        href={`/api/documents/${target.document}/file`}
        target="_blank"
        rel="noreferrer"
      >
        下载源文件
      </a>
    </aside>
  );
}
