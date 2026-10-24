"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, jsonBody, type ChatRequest } from "@/lib/api";
import { consumeStream } from "@/lib/stream";
import { id, text, rows, message, type Row } from "@/lib/data";
import { ErrorBox, Empty, list, useData } from "./ui";
import type { PageTarget } from "./preview";

export function Citations({
  value,
  preview,
}: {
  value: unknown;
  preview: (page: PageTarget) => void;
}) {
  return (
    <div className="citations">
      {value == null
        ? null
        : rows(value).map((row, index) => (
            <button
              key={index}
              className="citation"
              onClick={() =>
                preview({ document: id(row.document_id), page: id(row.page) })
              }
            >
              <span className="badge">{index + 1}</span>
              <span>
                <strong>{text(row.document_name)}</strong>
                <small>{text(row.quote)}</small>
              </span>
              <span>第 {text(row.page)} 页 ↗</span>
            </button>
          ))}
    </div>
  );
}
export function Chat({
  caseId,
  preview,
}: {
  caseId: number;
  preview: (page: PageTarget) => void;
}) {
  const conversations = useData(`/api/cases/${caseId}/conversations`);
  const [conversation, setConversation] = useState<number | null>(null);
  const [serial, next] = useState(0),
    [error, setError] = useState("");
  return (
    <section className="chat-layout">
      <aside className="card conversation-list">
        <button
          className="primary"
          onClick={() => {
            setConversation(null);
            next((n) => n + 1);
          }}
        >
          ＋ 新建问答
        </button>
        <ErrorBox error={conversations.error || error} />
        {list(conversations.data).map((row) => (
          <div key={id(row.id)}>
            <button
              className={conversation === row.id ? "selected" : ""}
              onClick={() => setConversation(id(row.id))}
            >
              {text(row.title) || `会话 ${row.id}`}
              {row.archived ? " · 已归档" : ""}
            </button>
            {!row.archived && (
              <button
                aria-label={`归档会话 ${row.id}`}
                onClick={async () => {
                  try {
                    await api(
                      `/api/conversations/${id(row.id)}/archive`,
                      jsonBody({}),
                    );
                    conversations.reload();
                    setConversation(null);
                    next((n) => n + 1);
                  } catch (e) {
                    setError(message(e));
                  }
                }}
              >
                归档
              </button>
            )}
          </div>
        ))}
      </aside>
      <ChatTurn
        key={`${caseId}:${conversation ?? `new-${serial}`}`}
        caseId={caseId}
        conversation={conversation}
        preview={preview}
        completed={() => conversations.reload()}
      />
    </section>
  );
}
function ChatTurn({
  caseId,
  conversation,
  preview,
  completed,
}: {
  caseId: number;
  conversation: number | null;
  preview: (page: PageTarget) => void;
  completed: () => void;
}) {
  const history = useData(
    conversation ? `/api/conversations/${conversation}/messages` : null,
  );
  const [messages, setMessages] = useState<Row[]>([]),
    [question, setQuestion] = useState(""),
    [busy, setBusy] = useState(false),
    [draft, setDraft] = useState(""),
    [phase, setPhase] = useState(""),
    [error, setError] = useState(""),
    [llm, setLlm] = useState(true);
  const [canRetry, setCanRetry] = useState(false);
  const active = useRef<AbortController | null>(null),
    currentConversation = useRef(conversation),
    pending = useRef<ChatRequest | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function send(retry = false) {
    if (busy) return;
    const payload: ChatRequest =
      retry && pending.current
        ? pending.current
        : {
            question: question.trim(),
            user_name: "本机律师",
            conversation_id: currentConversation.current,
            use_llm: llm,
            use_remote_embeddings: false,
            request_id: crypto.randomUUID(),
          };
    if (!payload.question) return;
    if (!retry)
      setMessages((old) => [
        ...old,
        { role: "user", content: payload.question },
      ]);
    pending.current = payload;
    setCanRetry(true);
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setDraft("");
    setPhase("检索中");
    setQuestion("");
    let final: Row | null = null,
      done = false,
      failure = "";
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          final = null;
          done = false;
          failure = "";
          setDraft("");
          const response = await fetch(`/api/cases/${caseId}/chat/stream`, {
            ...jsonBody(payload),
            signal: controller.signal,
            credentials: "same-origin",
          });
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            if (response.status === 401)
              window.dispatchEvent(new Event("lexvault:unauthorized"));
            throw new ApiError(
              response.status,
              typeof body.detail === "string"
                ? body.detail
                : `请求失败 (${response.status})`,
            );
          }
          if (
            !response.body ||
            !response.headers.get("content-type")?.includes("text/event-stream")
          )
            throw new Error("服务未返回流式协议");
          await consumeStream(response.body, (event) => {
            if (controller.signal.aborted) return;
            if (event.type === "status")
              setPhase(
                (
                  {
                    retrieval: "检索中",
                    generating: "生成中",
                    validating: "核验引用",
                    replayed: "恢复已完成问答",
                  } as Record<string, string>
                )[event.data.phase] || "处理中",
              );
            if (event.type === "token")
              setDraft((old) => old + event.data.text);
            if (event.type === "final") final = event.data;
            if (event.type === "retract") {
              setDraft("");
              final = null;
              failure = event.data.message;
            }
            if (event.type === "done") {
              done = event.data.status === "ok";
              if (!done) failure ||= "问答未完成，草稿已撤回";
            }
          });
          break;
        } catch (e) {
          if (
            controller.signal.aborted ||
            e instanceof ApiError ||
            attempt === 1
          )
            throw e;
          setPhase("连接中断，正在恢复");
        }
      }
      if (failure) throw new Error(failure);
      if (!done || !final) throw new Error("问答尚未确认，可使用同一请求重试");
      const result = final as Row;
      if (controller.signal.aborted) return;
      currentConversation.current = id(result.conversation_id);
      setMessages((old) => [
        ...old,
        { ...result, role: "assistant", content: result.answer },
      ]);
      pending.current = null;
      setCanRetry(false);
      completed();
    } catch (e) {
      if (!controller.signal.aborted) setError(message(e));
    } finally {
      if (!controller.signal.aborted) {
        setBusy(false);
        setDraft("");
        setPhase("");
      }
    }
  }
  return (
    <div className="card chat-panel">
      <header>
        <div>
          <span className="eyebrow">AI 阅卷</span>
          <h2>让每个回答都有出处</h2>
        </div>
        <label className="inline">
          <input
            type="checkbox"
            checked={llm}
            onChange={(e) => setLlm(e.target.checked)}
            disabled={busy}
          />
          使用模型
        </label>
      </header>
      <ErrorBox error={history.error || error} />
      <div className="messages" aria-live="polite">
        {!history.data && !messages.length && (
          <Empty>从卷宗开始提问。回答须经引用核验，并由律师复核。</Empty>
        )}
        {[...list(history.data), ...messages].map((row, index) => (
          <article
            key={index}
            className={`message ${row.role === "user" ? "user" : "assistant"}`}
          >
            <span className="eyebrow">
              {row.role === "user" ? "你" : "阅卷助手"}
            </span>
            <div className="answer">{text(row.content)}</div>
            {row.role !== "user" && (
              <Citations value={row.citations} preview={preview} />
            )}
          </article>
        ))}
        {busy && (
          <article className="message draft">
            <span className="badge">{phase} · 草稿待校验</span>
            <div className="answer">{draft || "正在查找相关材料…"}</div>
          </article>
        )}
      </div>
      {error && canRetry && !busy && (
        <button onClick={() => void send(true)}>使用同一请求重试</button>
      )}
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <label className="sr-only" htmlFor="question">
          向卷宗提问
        </label>
        <textarea
          id="question"
          placeholder="例如：关键事实有哪些？请列出原文出处。"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          maxLength={3000}
          disabled={busy}
          required
        />
        <div>
          <small>请核对引用原文；系统不代替律师判断。</small>
          {busy ? (
            <button
              type="button"
              onClick={() => {
                active.current?.abort();
                setBusy(false);
                setDraft("");
                setPhase("");
                setError("已停止接收，可用同一请求确认结果");
              }}
            >
              停止
            </button>
          ) : (
            <button className="primary" disabled={!question.trim()}>
              发送 ↑
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
