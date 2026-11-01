"use client";
import { useState } from "react";
import { rowsOf, type SearchHit } from "@/lib/types";
import { Empty, ErrorBox, useData } from "./ui";
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
  const hits = rowsOf<SearchHit>(data.data);
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
      {hits.map((hit) => (
        <article className="card" key={hit.page_id}>
          <h3>
            {hit.name} · 第 {hit.page_no} 页
          </h3>
          <p className="answer">{hit.text}</p>
          <button
            onClick={() =>
              preview({ document: hit.document_id, page: hit.page_no })
            }
          >
            查看原页
          </button>
        </article>
      ))}
      {submitted && !hits.length && (
        <Empty>{data.data ? "没有找到匹配材料" : "正在检索…"}</Empty>
      )}
    </section>
  );
}
