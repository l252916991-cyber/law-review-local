"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, jsonBody } from "@/lib/api";
import { id, message, object, text, type Row } from "@/lib/data";
import { Citations } from "./chat";
import { Usage } from "./metrics";
import { ErrorBox, Empty, list, useData } from "./ui";
import type { PageTarget } from "./preview";

export function Trace({ runId }: { runId: number }) {
  const data = useData(`/api/agent-runs/${runId}`);
  const run = data.data ? object(data.data) : {};
  return (
    <section className="card" aria-label="执行轨迹">
      <header>
        <h2>任务 #{runId} 执行轨迹</h2>
        <span className="badge">{text(run.status)}</span>
      </header>
      <ErrorBox error={data.error} />
      <p>仅展示节点结果、调用信息和失败原因，不展示隐藏思维链。</p>
      {list(run.steps).map((step, index) => {
        const output = step.output ? object(step.output) : {},
          input = step.input ? object(step.input) : {};
        const tools = output.tools ? object(output.tools) : {},
          route = output.model_route ? object(output.model_route) : {};
        return (
          <details className="trace-step" key={index}>
            <summary>
              <span className="badge">{index + 1}</span>{" "}
              {text(step.role || step.agent_role || step.node_name)} ·{" "}
              {text(step.latency_ms)}ms · {text(step.status)}
            </summary>
            <p>{text(output.summary)}</p>
            <dl>
              <dt>输入问题</dt>
              <dd>{text(input.question || input.query) || "未记录"}</dd>
              <dt>结果</dt>
              <dd className="answer">
                {text(output.answer || output.summary) || "无文本结果"}
              </dd>
              <dt>失败 / 校验</dt>
              <dd>
                {text(step.error || output.error) ||
                  (output.diagnostic
                    ? text(object(output.diagnostic).code)
                    : "") ||
                  (Array.isArray(output.issues)
                    ? output.issues.map(text).join("；")
                    : "无")}
              </dd>
              <dt>模型回退</dt>
              <dd>
                {route.fallback
                  ? `${text(route.model)} · ${text(route.reason)}`
                  : "未回退"}
              </dd>
            </dl>
            {list(tools.trace).map((tool, i) => (
              <div className="row" key={i}>
                <span>
                  {text(tool.tool)} · {tool.ok ? "成功" : "拒绝"}
                </span>
                <span>
                  {text(tool.latency_ms)}ms · {text(tool.count)} 条
                </span>
              </div>
            ))}
          </details>
        );
      })}
      {run.usage ? <Usage summary={object(run.usage)} /> : null}
    </section>
  );
}

export function Agent({
  caseId,
  preview,
}: {
  caseId: number;
  preview: (page: PageTarget) => void;
}) {
  const platform = useData(`/api/cases/${caseId}/platform-metrics`);
  const [result, setResult] = useState<Row | null>(null),
    [comparison, setComparison] = useState<Row | null>(null),
    [trace, setTrace] = useState<number | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [status, setStatus] = useState("等待运行"),
    [llm, setLlm] = useState(true),
    [mode, setMode] = useState("multi_agent"),
    [question, setQuestion] = useState(""),
    [asyncJob, setAsyncJob] = useState(true);
  const controller = useRef<AbortController | null>(null),
    jobKey = `lexvault-next-job:${caseId}`;
  useEffect(() => () => controller.current?.abort(), []);
  async function track(jobId: string, signal: AbortSignal) {
    for (;;) {
      const job = object(
        await api(`/api/agent-jobs/${encodeURIComponent(jobId)}`, { signal }),
      );
      if (signal.aborted) return;
      setStatus(`后台任务 ${text(job.status)}`);
      if (job.run_id) setTrace(id(job.run_id));
      if (job.status === "completed") {
        sessionStorage.removeItem(jobKey);
        setResult(object(job.result));
        platform.reload();
        return;
      }
      if (["failed", "interrupted"].includes(text(job.status))) {
        sessionStorage.removeItem(jobKey);
        throw new ApiError(
          500,
          job.error ? text(object(job.error).message) : "任务中断",
          job,
        );
      }
      await new Promise<void>((resolve, reject) => {
        const cancel = () => {
          clearTimeout(timer);
          reject(new DOMException("Aborted", "AbortError"));
        };
        const timer = setTimeout(() => {
          signal.removeEventListener("abort", cancel);
          resolve();
        }, 1200);
        signal.addEventListener("abort", cancel, { once: true });
      });
    }
  }
  async function run(
    action: "run" | "compare" | "track" | "resume",
    runId?: number,
  ) {
    if (busy) return;
    const active = new AbortController();
    controller.current = active;
    setBusy(true);
    setError("");
    setResult(null);
    setComparison(null);
    setStatus("执行中…");
    setTrace(runId || null);
    try {
      const payload = {
        question,
        mode,
        use_llm: llm,
        use_remote_embeddings: false,
      };
      if (action === "track") {
        const saved = sessionStorage.getItem(jobKey);
        if (!saved) throw new Error("本案没有待跟踪后台任务");
        await track(saved, active.signal);
      } else if (action === "resume") {
        const value = object(
          await api(`/api/agent-runs/${runId}/resume`, {
            method: "POST",
            signal: active.signal,
          }),
        );
        if (!active.signal.aborted) setResult(value);
      } else if (action === "compare") {
        const value = object(
          await api(`/api/cases/${caseId}/agent-compare`, {
            ...jsonBody(payload),
            signal: active.signal,
          }),
        );
        if (!active.signal.aborted) setComparison(value);
      } else if (asyncJob) {
        const value = object(
          await api(`/api/cases/${caseId}/agent-jobs`, {
            ...jsonBody(payload),
            signal: active.signal,
          }),
        );
        sessionStorage.setItem(jobKey, text(value.job_id));
        await track(text(value.job_id), active.signal);
      } else {
        const value = object(
          await api(`/api/cases/${caseId}/agent-chat`, {
            ...jsonBody(payload),
            signal: active.signal,
          }),
        );
        if (!active.signal.aborted) {
          setResult(value);
          if (value.run_id) setTrace(id(value.run_id));
        }
      }
      if (!active.signal.aborted) {
        setStatus("执行完成");
        platform.reload();
      }
    } catch (e) {
      if (!active.signal.aborted) {
        setError(message(e));
        setStatus("执行失败");
        if (e instanceof ApiError && e.detail) {
          const detail = object(e.detail);
          if (detail.run_id) setTrace(id(detail.run_id));
        }
      }
    } finally {
      if (!active.signal.aborted) setBusy(false);
    }
  }
  const metrics = platform.data ? object(platform.data) : {};
  return (
    <section>
      <header className="section-header">
        <div>
          <span className="eyebrow">Agent 工作台</span>
          <h2>按步骤完成审校</h2>
          <p>查看检索、只读工具、审校和引用校验的运行结果。</p>
        </div>
        <span role="status" className="badge">
          {status}
        </span>
      </header>
      <ErrorBox error={error || platform.error} />
      <form
        className="card"
        onSubmit={(e) => {
          e.preventDefault();
          void run("run");
        }}
      >
        <label>
          审校问题
          <textarea
            required
            maxLength={3000}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
        </label>
        <div className="controls">
          <label>
            分析方式
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value)}
              disabled={busy}
            >
              <option value="multi_agent">标准分析 · 原生 DAG</option>
              <option value="langgraph">可恢复分析 · LangGraph</option>
            </select>
          </label>
          <label className="inline">
            <input
              type="checkbox"
              checked={llm}
              onChange={(e) => setLlm(e.target.checked)}
              disabled={busy}
            />
            使用模型
          </label>
          <label className="inline">
            <input
              type="checkbox"
              checked={asyncJob}
              onChange={(e) => setAsyncJob(e.target.checked)}
              disabled={busy}
            />
            后台执行
          </label>
        </div>
        <button className="primary" disabled={busy || !question.trim()}>
          运行审校 →
        </button>
        <button
          type="button"
          disabled={busy || !question.trim()}
          onClick={() => void run("compare")}
        >
          分析方式对比
        </button>
        <button type="button" disabled={busy} onClick={() => void run("track")}>
          继续跟踪后台任务
        </button>
        {busy && (
          <button
            type="button"
            onClick={() => {
              controller.current?.abort();
              setBusy(false);
              setStatus("已停止跟踪；后台任务可能继续运行");
            }}
          >
            停止跟踪
          </button>
        )}
      </form>
      {result && (
        <section className="card" aria-label="审校结果">
          <h2>审校结果</h2>
          <span className="badge">
            {text(result.route)} · {text(result.llm_model)} ·{" "}
            {text(result.total_ms)}ms
          </span>
          {result.model_route && object(result.model_route).fallback ? (
            <p>
              回退模型：{text(object(result.model_route).model)} ·{" "}
              {text(object(result.model_route).reason)}
            </p>
          ) : null}
          <div className="answer">{text(result.answer)}</div>
          <Citations value={result.citations} preview={preview} />
        </section>
      )}
      {comparison && (
        <section className="card">
          <h2>运行方式对比</h2>
          {(["native", "langgraph"] as const).map((key) => {
            const item = object(comparison[key]);
            return (
              <article key={key}>
                <h3>
                  {key} · {text(item.total_ms)}ms
                </h3>
                <div className="answer">{text(item.answer)}</div>
                <Citations value={item.citations} preview={preview} />
              </article>
            );
          })}
          <p>差异：{JSON.stringify(comparison.comparison)}</p>
        </section>
      )}
      {trace ? (
        <Trace key={`${trace}:${status}`} runId={trace} />
      ) : (
        <Empty>执行后显示节点轨迹</Empty>
      )}
      <section className="card">
        <h2>最近运行</h2>
        {list(metrics.recent_runs).map((row) => (
          <div className="row" key={id(row.id)}>
            <span>
              #{text(row.id)} · {text(row.question)}
              <small>
                {text(row.status)} · {text(row.total_ms)}ms
              </small>
            </span>
            <div>
              <button onClick={() => setTrace(id(row.id))}>查看轨迹</button>
              {row.resumable ? (
                <button
                  disabled={busy}
                  onClick={() => void run("resume", id(row.id))}
                >
                  恢复运行
                </button>
              ) : null}
            </div>
          </div>
        ))}
      </section>
      <Quality caseId={caseId} />
    </section>
  );
}
function Quality({ caseId }: { caseId: number }) {
  const [truth, setTruth] = useState(""),
    [report, setReport] = useState<Row | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  async function check(index: boolean) {
    setBusy(true);
    setError("");
    try {
      const value = await api(
        `/api/cases/${caseId}/${index ? "vector-index" : "evaluate-rag"}`,
        index || !truth.trim()
          ? { method: "POST" }
          : jsonBody(JSON.parse(truth)),
      );
      setReport(object(value));
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <details className="card">
      <summary>检索质量与向量索引</summary>
      <p>页级检索指标不等同于答案质量；非演示案件须填写本案标准答案。</p>
      <label>
        本案标准答案 JSON
        <textarea
          placeholder={'[{"query":"问题","expected":[["文件名.txt",1]]}]'}
          value={truth}
          onChange={(e) => setTruth(e.target.value)}
        />
      </label>
      <button disabled={busy} onClick={() => void check(false)}>
        检查检索质量
      </button>
      <button disabled={busy} onClick={() => void check(true)}>
        更新检索索引
      </button>
      <ErrorBox error={error} />
      {report && <pre>{JSON.stringify(report, null, 2)}</pre>}
    </details>
  );
}
