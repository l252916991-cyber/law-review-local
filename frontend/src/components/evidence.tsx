"use client";
import { useState } from "react";
import { ApiError, api, jsonBody } from "@/lib/api";
import { id, message, object, text, type Row } from "@/lib/data";
import { Empty, ErrorBox, Modal, list, useData, values } from "./ui";
import type { PageTarget } from "./preview";
import { materialTimeline } from "@/lib/timeline";

const categories = [
  "书证",
  "物证",
  "言词证据",
  "银行流水",
  "审计报告",
  "询问笔录",
  "电子数据",
  "其他材料",
];
const statuses = ["待复核", "已确认", "待质证", "待补证"];
function Select({
  name,
  label,
  options,
  value,
}: {
  name: string;
  label: string;
  options: string[];
  value?: unknown;
}) {
  return (
    <label>
      {label}
      <select
        aria-label={label}
        name={name}
        defaultValue={text(value) || options[0]}
      >
        {options.map((item) => (
          <option key={item}>{item}</option>
        ))}
      </select>
    </label>
  );
}
export function Evidence({
  caseId,
  preview,
}: {
  caseId: number;
  preview: (page: PageTarget) => void;
}) {
  const data = useData(`/api/cases/${caseId}/evidence`),
    documents = useData(`/api/cases/${caseId}/documents`),
    gaps = useData(`/api/cases/${caseId}/gap-analysis`);
  const [edit, setEdit] = useState<Row | null>(null),
    [annotation, setAnnotation] = useState<number | null>(null),
    [error, setError] = useState(""),
    [filter, setFilter] = useState(""),
    [busy, setBusy] = useState(false),
    [highlight, setHighlight] = useState<number | null>(null);
  const result = data.data ? object(data.data) : {},
    items = list(result.evidence),
    relations = list(result.relations),
    gapData = gaps.data ? object(gaps.data) : {};
  async function perform(path: string, method = "POST") {
    setBusy(true);
    setError("");
    try {
      await api(path, jsonBody({}, method));
      data.reload();
      gaps.reload();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  const connected = new Set<number>(highlight ? [highlight] : []);
  if (highlight) {
    for (let n = 0; n < items.length; n++)
      for (const relation of relations) {
        const from = Number(relation.from_evidence_id),
          to = Number(relation.to_evidence_id);
        if (connected.has(from) || connected.has(to)) {
          connected.add(from);
          connected.add(to);
        }
      }
  }
  return (
    <section>
      <header className="section-header">
        <div>
          <span className="eyebrow">证据审查</span>
          <h2>证据事项与关联</h2>
        </div>
        <div>
          <button onClick={() => setEdit({})}>＋ 添加证据</button>
          <button
            className="primary"
            disabled={busy}
            onClick={() => void perform(`/api/cases/${caseId}/analyze`)}
          >
            分析证据
          </button>
        </div>
      </header>
      <ErrorBox error={error || data.error} />
      <label>
        筛选证据
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="标题、事实或状态"
        />
      </label>
      <section className="card">
        <header>
          <h3>证据关系图谱</h3>
          <button onClick={() => setHighlight(null)}>显示全部</button>
        </header>
        <p>选择节点追踪关联链；点击下方原文按钮核对材料。</p>
        <div className="graph-scroll">
          <svg
            role="img"
            aria-label="证据关联图"
            viewBox={`0 0 960 ${Math.max(150, Math.ceil(items.length / 3) * 100)}`}
            style={{ width: "100%", minWidth: 620 }}
          >
            {relations.map((r, i) => {
              const from = items.findIndex((e) => e.id === r.from_evidence_id),
                to = items.findIndex((e) => e.id === r.to_evidence_id);
              return from < 0 || to < 0 ? null : (
                <g key={i}>
                  <path
                    d={`M${(from % 3) * 320 + 140},${Math.floor(from / 3) * 100 + 38} L${(to % 3) * 320 + 140},${Math.floor(to / 3) * 100 + 38}`}
                    stroke="#8ca68b"
                    strokeWidth="2"
                  />
                  <title>
                    {text(r.relation_type)}：{text(r.note)}
                  </title>
                </g>
              );
            })}
            {items.map((row, i) => (
              <g
                key={id(row.id)}
                role="button"
                aria-label={`追踪证据：${text(row.title)}`}
                tabIndex={0}
                onClick={() => setHighlight(id(row.id))}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setHighlight(id(row.id));
                  }
                }}
              >
                <title>{text(row.title)}</title>
                <rect
                  x={(i % 3) * 320 + 10}
                  y={Math.floor(i / 3) * 100 + 10}
                  width={260}
                  height={55}
                  rx={8}
                  fill={connected.has(id(row.id)) ? "#dee8cc" : "#f4f6ee"}
                  stroke="#59725b"
                />
                <text
                  x={(i % 3) * 320 + 24}
                  y={Math.floor(i / 3) * 100 + 43}
                  fontSize={13}
                  fill="#294739"
                >
                  {text(row.title).slice(0, 17)}
                  {text(row.title).length > 17 ? "…" : ""}
                </text>
              </g>
            ))}
          </svg>
        </div>
        <ul>
          {relations.map((row, i) => (
            <li key={i}>
              #{text(row.from_evidence_id)} → #{text(row.to_evidence_id)} ·{" "}
              {text(row.relation_type)} · {text(row.note)}
            </li>
          ))}
        </ul>
        {!items.length && <Empty>尚无证据关系</Empty>}
      </section>
      <div className="evidence-grid">
        {items
          .filter(
            (row) =>
              (!filter ||
                `${text(row.title)} ${text(row.fact)} ${text(row.status)}`.includes(
                  filter,
                )) &&
              (!highlight || connected.has(id(row.id))),
          )
          .map((row) => (
            <article className="card" key={id(row.id)}>
              <header>
                <span className="badge">{text(row.category)}</span>
                <span className="badge">{text(row.status)}</span>
              </header>
              <h3>{text(row.title)}</h3>
              <p>{text(row.fact)}</p>
              <blockquote>{text(row.quote)}</blockquote>
              <small>
                {text(row.source_name)} · 第 {text(row.source_page_start)}–
                {text(row.source_page_end)} 页 · 可信度 {text(row.credibility)}
              </small>
              {row.approved_by ? (
                <small>
                  确认人：{text(row.approved_by)} · {text(row.approved_at)}
                </small>
              ) : null}
              <div className="controls">
                <button onClick={() => setEdit(row)}>编辑证据</button>
                <button onClick={() => setAnnotation(id(row.id))}>批注</button>
                {row.source_document_id ? (
                  <button
                    onClick={() =>
                      preview({
                        document: id(row.source_document_id),
                        page: id(row.source_page_start),
                      })
                    }
                  >
                    回到原文
                  </button>
                ) : null}
                <button
                  onClick={() => {
                    if (
                      window.confirm(
                        "确认删除该证据？关联关系及批注将按服务端规则处理。",
                      )
                    )
                      void perform(`/api/evidence/${id(row.id)}`, "DELETE");
                  }}
                >
                  删除证据
                </button>
              </div>
            </article>
          ))}
      </div>
      <details className="card">
        <summary>材料日期时间线</summary>
        <p>
          按来源卷宗所载日期排列，保留时间范围精度；未记录日期的不推断为事实发生时间。
        </p>
        {materialTimeline(items, list(documents.data)).map((item) => (
          <div className="row" key={id(item.evidence.id)}>
            <time>{item.dateRange || "未记录日期"}</time>
            <span>
              {text(item.evidence.title)}
              <small>{text(item.evidence.category)}</small>
            </span>
          </div>
        ))}
      </details>
      <details className="card">
        <summary>证据录入时间线</summary>
        <p>按系统录入时间排列，不推断事实发生时间。</p>
        {[...items]
          .sort((a, b) => text(a.created_at).localeCompare(text(b.created_at)))
          .map((row) => (
            <div className="row" key={id(row.id)}>
              <time>{text(row.created_at) || "未知"}</time>
              <span>{text(row.title)}</span>
            </div>
          ))}
      </details>
      <section className="card">
        <header>
          <h2>证据缺口</h2>
          <button
            disabled={busy}
            onClick={() =>
              void perform(`/api/cases/${caseId}/gap-analysis/detect`)
            }
          >
            运行疏漏检测
          </button>
        </header>
        <ErrorBox error={gaps.error} />
        {list(gapData.gaps).map((row, i) => (
          <article className="row" key={i}>
            <span className="badge">{text(row.severity)}</span>
            <div>
              <h3>{text(row.title || row.gap_type)}</h3>
              <p>{text(row.description)}</p>
              <small>{text(row.suggestion || row.recommendation)}</small>
            </div>
          </article>
        ))}
        {!list(gapData.gaps).length && (
          <Empty>暂无已记录缺口；可运行检测。</Empty>
        )}
      </section>
      {edit && (
        <Modal
          title={edit.id ? "编辑证据" : "添加证据"}
          close={() => setEdit(null)}
        >
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const body: Record<string, unknown> = values(e.currentTarget);
              for (const field of ["source_page_start", "source_page_end"])
                body[field] = Number(body[field]);
              body.source_document_id = body.source_document_id
                ? Number(body.source_document_id)
                : null;
              if (edit.id) body.expected_version = Number(edit.version);
              try {
                await api(
                  edit.id
                    ? `/api/evidence/${id(edit.id)}`
                    : `/api/cases/${caseId}/evidence`,
                  jsonBody(body, edit.id ? "PATCH" : "POST"),
                );
                setEdit(null);
                data.reload();
              } catch (e) {
                if (e instanceof ApiError && e.status === 409) {
                  // 同事已先保存：关闭编辑并刷新，不基于旧版本继续提交。
                  setEdit(null);
                  data.reload();
                }
                setError(message(e));
              }
            }}
          >
            <label>
              证据标题
              <input
                name="title"
                required
                minLength={2}
                maxLength={200}
                defaultValue={text(edit.title)}
              />
            </label>
            <label>
              待证事实
              <textarea name="fact" required defaultValue={text(edit.fact)} />
            </label>
            <div className="fields">
              <Select
                name="category"
                label="类别"
                options={categories}
                value={edit.category}
              />
              <Select
                name="status"
                label="状态"
                options={statuses}
                value={edit.status}
              />
              <Select
                name="credibility"
                label="可信度"
                options={["待核验", "高", "较高", "中", "低"]}
                value={edit.credibility}
              />
              <label>
                来源卷宗
                <select
                  name="source_document_id"
                  defaultValue={text(edit.source_document_id)}
                >
                  <option value="">无来源卷宗</option>
                  {list(documents.data).map((row) => (
                    <option value={id(row.id)} key={id(row.id)}>
                      {text(row.name)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                起始页
                <input
                  name="source_page_start"
                  type="number"
                  required
                  min={1}
                  defaultValue={Number(edit.source_page_start) || 1}
                />
              </label>
              <label>
                结束页
                <input
                  name="source_page_end"
                  type="number"
                  required
                  min={1}
                  defaultValue={Number(edit.source_page_end) || 1}
                />
              </label>
            </div>
            <label>
              原文引用
              <textarea name="quote" defaultValue={text(edit.quote)} />
            </label>
            <ErrorBox error={error} />
            <button className="primary">保存证据</button>
          </form>
        </Modal>
      )}
      {annotation && (
        <Annotations
          evidenceId={annotation}
          close={() => setAnnotation(null)}
        />
      )}
    </section>
  );
}
function Annotations({
  evidenceId,
  close,
}: {
  evidenceId: number;
  close: () => void;
}) {
  const data = useData(`/api/evidence/${evidenceId}/annotations`),
    [error, setError] = useState("");
  return (
    <Modal title="证据批注" close={close}>
      <ErrorBox error={error || data.error} />
      {list(data.data).map((row) => (
        <article className="row" key={id(row.id)}>
          <div>
            <strong>
              {text(row.annotation_type)} · {text(row.status)}
            </strong>
            <p>{text(row.content)}</p>
            <small>{text(row.user_name)}</small>
            <button
              onClick={async () => {
                try {
                  await api(
                    `/api/evidence-annotations/${id(row.id)}`,
                    jsonBody(
                      { status: row.status === "已处理" ? "待处理" : "已处理" },
                      "PATCH",
                    ),
                  );
                  data.reload();
                } catch (e) {
                  setError(message(e));
                }
              }}
            >
              切换处理状态
            </button>
            <button
              onClick={async () => {
                if (!window.confirm("确认删除批注？")) return;
                try {
                  await api(`/api/evidence-annotations/${id(row.id)}`, {
                    method: "DELETE",
                  });
                  data.reload();
                } catch (e) {
                  setError(message(e));
                }
              }}
            >
              删除批注
            </button>
          </div>
        </article>
      ))}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const form = e.currentTarget;
          try {
            await api(
              `/api/evidence/${evidenceId}/annotations`,
              jsonBody(values(form)),
            );
            form.reset();
            data.reload();
          } catch (e) {
            setError(message(e));
          }
        }}
      >
        <Select
          label="批注类型"
          name="annotation_type"
          options={["备注", "疑点", "质证意见", "补证建议", "重点", "其他"]}
        />
        <label>
          批注内容
          <textarea name="content" required maxLength={10000} />
        </label>
        <button className="primary">添加批注</button>
      </form>
    </Modal>
  );
}
