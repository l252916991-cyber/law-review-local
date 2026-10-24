"use client";
import { useState } from "react";
import { id, text } from "@/lib/data";
import { Empty, ErrorBox, list, useData } from "./ui";
import type { PageTarget } from "./preview";

export function Search({
  caseId,
  preview,
}: {
  caseId: number;
  preview: (page: PageTarget) => void;
}) {
  const [query, setQuery] = useState(""),
    [submitted, submit] = useState("");
  const data = useData(
    submitted
      ? `/api/cases/${caseId}/search?q=${encodeURIComponent(submitted)}`
      : null,
  );
  return (
    <section>
      <header>
        <span className="eyebrow">全文检索</span>
        <h2>在本案卷宗中查找</h2>
      </header>
      <form
        className="card"
        onSubmit={(e) => {
          e.preventDefault();
          submit(query.trim());
        }}
      >
        <label>
          检索内容
          <input
            maxLength={500}
            required
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button className="primary">搜索卷宗原文</button>
      </form>
      <ErrorBox error={data.error} />
      {list(data.data).map((row, index) => (
        <article className="card" key={index}>
          <h3>
            {text(row.name || row.document_name)} · 第{" "}
            {text(row.page_no || row.page)} 页
          </h3>
          <p className="answer">{text(row.text || row.quote)}</p>
          <button
            onClick={() =>
              preview({
                document: id(row.document_id),
                page: id(row.page_no || row.page),
              })
            }
          >
            查看原页
          </button>
        </article>
      ))}
      {submitted && !list(data.data).length && (
        <Empty>{data.data ? "没有找到匹配材料" : "正在检索…"}</Empty>
      )}
    </section>
  );
}
