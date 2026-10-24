"use client";
import { useEffect, useState } from "react";
import { api, jsonBody } from "@/lib/api";
import { id, message, object, text, type Row } from "@/lib/data";
import { Auth } from "./auth";
import { Chat } from "./chat";
import { Documents } from "./documents";
import { Preview, type PageTarget } from "./preview";
import { Metrics } from "./metrics";
import { Agent } from "./agent";
import { Evidence } from "./evidence";
import { Settings } from "./settings";
import { ExportCase } from "./export";
import { Search } from "./search";
import { Empty, ErrorBox, Fields, Modal, list, useData, values } from "./ui";

export default function Workspace() {
  return (
    <Auth>
      {(identity, logout) => <Shell identity={identity} logout={logout} />}
    </Auth>
  );
}
function Shell({
  identity,
  logout,
}: {
  identity: Row;
  logout: () => Promise<void>;
}) {
  const cases = useData("/api/cases");
  const [selected, select] = useState<number | null>(() => {
      if (typeof window === "undefined") return null;
      const value = Number(
        new URLSearchParams(window.location.search).get("case"),
      );
      return Number.isSafeInteger(value) && value > 0 ? value : null;
    }),
    [search, setSearch] = useState(""),
    [create, setCreate] = useState(false),
    [storage, setStorage] = useState<"archived" | "trash" | null>(null),
    [error, setError] = useState("");
  const allCases = list(cases.data),
    current = allCases.find((row) => row.id === selected) || allCases[0];
  return (
    <div className="workspace">
      <a href="#main" className="skip">
        跳至主要内容
      </a>
      <aside className="sidebar">
        <div className="brand">
          律{" "}
          <span>
            LexVault<small>律师阅卷工作台</small>
          </span>
        </div>
        <p className="workspace-label">
          {text(identity.organization_name) || "案件工作空间"}
        </p>
        <input
          aria-label="搜索案件"
          placeholder="搜索案件…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <nav aria-label="案件列表">
          {allCases
            .filter((row) => text(row.title).includes(search))
            .map((row) => (
              <button
                key={id(row.id)}
                className={row.id === current?.id ? "selected" : ""}
                onClick={() => select(id(row.id))}
              >
                <strong>{text(row.title)}</strong>
                <small>
                  {text(row.case_no) || "未填写案号"} ·{" "}
                  {text(row.document_count)} 份卷宗
                </small>
              </button>
            ))}
        </nav>
        {identity.admin ? (
          <button className="primary" onClick={() => setCreate(true)}>
            ＋ 新建案件
          </button>
        ) : null}
        <div className="sidebar-bottom">
          <button onClick={() => setStorage("archived")}>已归档案卷</button>
          <button onClick={() => setStorage("trash")}>回收站</button>
          <p>
            {text(identity.name)}
            <button
              onClick={() => void logout().catch((e) => setError(message(e)))}
            >
              退出
            </button>
          </p>
        </div>
      </aside>
      <div className="workspace-main">
        <ErrorBox error={error || cases.error} />
        {current ? (
          <CaseWorkspace
            key={id(current.id)}
            caseRow={current}
            refresh={cases.reload}
          />
        ) : (
          <main id="main">
            <Empty>
              {cases.data ? "尚无案件，创建案件后导入材料。" : "正在加载案件…"}
            </Empty>
          </main>
        )}
      </div>
      {create && (
        <Modal title="新建案件" close={() => setCreate(false)}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const payload = values(e.currentTarget);
              try {
                const created = object(
                  await api("/api/cases", jsonBody(payload)),
                );
                select(id(created.id));
                cases.reload();
                setCreate(false);
              } catch (e) {
                setError(message(e));
              }
            }}
          >
            <label>
              案件名称
              <input name="title" required maxLength={200} />
            </label>
            <Fields
              names={{
                case_no: "案号",
                case_type: "案件类型",
                client_name: "委托人",
                description: "案件说明",
              }}
            />
            <ErrorBox error={error} />
            <button className="primary">创建案件</button>
          </form>
        </Modal>
      )}
      {storage && (
        <Storage
          status={storage}
          close={() => setStorage(null)}
          restored={() => {
            cases.reload();
            setStorage(null);
          }}
        />
      )}
    </div>
  );
}
function Storage({
  status,
  close,
  restored,
}: {
  status: "archived" | "trash";
  close: () => void;
  restored: () => void;
}) {
  const data = useData(`/api/cases/lifecycle/${status}`);
  const [error, setError] = useState("");
  return (
    <Modal title={status === "trash" ? "回收站" : "已归档案卷"} close={close}>
      <ErrorBox error={error || data.error} />
      {list(data.data).map((row) => (
        <article className="row" key={id(row.id)}>
          <span>
            {text(row.title)}
            <small>{text(row.purge_after)}</small>
          </span>
          <button
            onClick={async () => {
              try {
                await api(`/api/cases/${id(row.id)}/restore`, jsonBody({}));
                restored();
              } catch (e) {
                setError(message(e));
              }
            }}
          >
            恢复案件
          </button>
        </article>
      ))}
      {!list(data.data).length && <Empty />}
    </Modal>
  );
}
function CaseWorkspace({
  caseRow,
  refresh,
}: {
  caseRow: Row;
  refresh: () => void;
}) {
  const caseId = id(caseRow.id),
    detail = useData(`/api/cases/${caseId}`),
    audit = useData(`/api/cases/${caseId}/audit`);
  const [view, setView] = useState(() => {
      const value =
        typeof window === "undefined"
          ? ""
          : new URLSearchParams(window.location.search).get("view");
      return [
        "overview",
        "documents",
        "evidence",
        "chat",
        "agent",
        "metrics",
        "search",
        "export",
        "settings",
      ].includes(value || "")
        ? value!
        : "overview";
    }),
    [page, setPage] = useState<PageTarget | null>(null),
    [error, setError] = useState("");
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("case", String(caseId));
    url.searchParams.set("view", view);
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }, [caseId, view]);
  useEffect(() => {
    Promise.resolve().then(() => {
      try {
        setDark(localStorage.getItem("lexvault-next-theme") === "dark");
      } catch {
        /* Storage may be disabled. */
      }
    });
    const keyboard = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "k") {
        event.preventDefault();
        document
          .querySelector<HTMLInputElement>('[aria-label="搜索案件"]')
          ?.focus();
      }
      if (event.altKey && ["1", "2", "3", "4"].includes(event.key)) {
        event.preventDefault();
        setView(
          ["overview", "documents", "evidence", "chat"][Number(event.key) - 1],
        );
        setPage(null);
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, []);
  const record = detail.data ? object(detail.data) : caseRow,
    metrics = record.metrics ? object(record.metrics) : {};
  const views: Record<string, string> = {
    overview: "案件总览",
    documents: "卷宗目录",
    evidence: "证据关系",
    chat: "AI 阅卷",
    agent: "阅卷任务",
    metrics: "用量与费用",
    search: "全文检索",
    export: "成果导出",
    settings: "模型设置",
  };
  return (
    <div className={dark ? "case-space dark" : "case-space"}>
      <header className="topbar">
        <div>
          <span className="eyebrow">
            {text(record.case_no) || "案件工作空间"}
          </span>
          <h1>{text(record.title)}</h1>
        </div>
        <div>
          <span className="badge">{text(record.status) || "阅卷中"}</span>
          <button
            aria-label="切换主题"
            onClick={() =>
              setDark((value) => {
                try {
                  localStorage.setItem(
                    "lexvault-next-theme",
                    value ? "light" : "dark",
                  );
                } catch {
                  /* Storage may be disabled. */
                }
                return !value;
              })
            }
          >
            {dark ? "浅色" : "深色"}
          </button>
        </div>
      </header>
      <nav className="tabs" aria-label="工作区导航">
        {Object.entries(views).map(([key, label]) => (
          <button
            key={key}
            aria-current={view === key ? "page" : undefined}
            onClick={() => {
              setView(key);
              setPage(null);
            }}
          >
            {label}
          </button>
        ))}
      </nav>
      <main id="main">
        <ErrorBox error={error || detail.error} />
        {view === "overview" && (
          <>
            <section className="hero">
              <span className="eyebrow">本案阅卷</span>
              <h2>从材料出发，让判断有据可查。</h2>
              <p>
                {text(record.description) ||
                  "导入卷宗、梳理证据，带着问题回到原文。"}
              </p>
              <button className="primary" onClick={() => setView("chat")}>
                开始阅卷 →
              </button>
              <button onClick={() => setView("documents")}>添加案件材料</button>
            </section>
            <div className="metrics">
              {Object.entries({
                documents: "卷宗文件",
                pages: "卷宗页数",
                evidence: "证据事项",
                confirmed: "已确认证据",
              }).map(([key, label]) => (
                <article className="card" key={key}>
                  <span>{label}</span>
                  <strong>{text(metrics[key]) || "—"}</strong>
                </article>
              ))}
            </div>
            <section className="card">
              <header>
                <h2>最近活动</h2>
                <span className="badge">操作留痕</span>
              </header>
              <ErrorBox error={audit.error} />
              {list(audit.data)
                .slice(0, 12)
                .map((row, index) => (
                  <div className="row" key={index}>
                    <span>
                      {text(row.action)}
                      <small>{text(row.detail)}</small>
                    </span>
                    <time>{text(row.created_at)}</time>
                  </div>
                ))}
            </section>
            <section className="card">
              <h2>案卷管理</h2>
              <p>归档保留材料；移入回收站后按现有保留策略处理。</p>
              {(["archive", "trash"] as const).map((action) => (
                <button
                  key={action}
                  onClick={async () => {
                    if (
                      !window.confirm(
                        action === "archive"
                          ? "确认归档本案？"
                          : "确认将本案移入回收站？",
                      )
                    )
                      return;
                    try {
                      await api(`/api/cases/${caseId}/${action}`, jsonBody({}));
                      refresh();
                    } catch (e) {
                      setError(message(e));
                    }
                  }}
                >
                  {action === "archive" ? "归档案卷" : "移入回收站"}
                </button>
              ))}
            </section>
          </>
        )}
        {view === "documents" && (
          <Documents caseId={caseId} preview={setPage} />
        )}
        {view === "chat" && <Chat caseId={caseId} preview={setPage} />}
        {view === "evidence" && <Evidence caseId={caseId} preview={setPage} />}
        {view === "agent" && <Agent caseId={caseId} preview={setPage} />}
        {view === "metrics" && <Metrics caseId={caseId} />}
        {view === "export" && <ExportCase caseId={caseId} />}
        {view === "settings" && <Settings />}
        {view === "search" && <Search caseId={caseId} preview={setPage} />}
      </main>
      {page && (
        <Preview
          key={`${page.document}:${page.page}`}
          target={page}
          close={() => setPage(null)}
          select={setPage}
        />
      )}
    </div>
  );
}
