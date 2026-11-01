"use client";
import { useState } from "react";
import { ApiError, api, jsonBody } from "@/lib/api";
import { id, message, object, strings, text, type Row } from "@/lib/data";
import { Empty, ErrorBox, list, useData } from "./ui";

const KINDS = ["阅卷", "核证", "补证", "复核", "归档", "其他"];
const ACTION_LABELS: Record<string, string> = {
  create: "创建任务",
  start: "开始处理",
  submit: "提交复核",
  return: "退回",
  complete: "确认完成",
  cancel: "取消",
  assign: "调整负责人",
  due: "调整期限",
};

function actionLabel(action: unknown) {
  return ACTION_LABELS[text(action)] || text(action);
}

function flag(value: unknown, fallback: boolean) {
  return typeof value === "boolean" ? value : fallback;
}

function lastEventText(task: Row) {
  const events = list(task.events);
  if (!events.length) return "";
  const last = events[events.length - 1];
  return ` · ${text(last.actor)} ${actionLabel(last.action)}`;
}

export function ReviewTasks({ caseId, identity }: { caseId: number; identity: Row }) {
  const [mine, setMine] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const tasks = useData(`/api/cases/${caseId}/tasks${mine ? "?mine=true" : ""}`),
    roles = identity.roles ? object(identity.roles) : {},
    role = text(roles[String(caseId)]),
    permissions = strings(identity.permissions),
    manager = Boolean(identity.admin) || (permissions.includes("edit") && role === "owner"),
    reviewer = Boolean(identity.admin) || (permissions.includes("approve") && ["owner", "reviewer"].includes(role)),
    canWork = Boolean(identity.admin) || permissions.includes("edit"),
    members = useData(manager ? `/api/cases/${caseId}/task-assignees` : null);
  const body = tasks.data ? object(tasks.data) : {},
    items = list(body.tasks),
    counts = body.counts ? object(body.counts) : {},
    candidates = list(members.data ? object(members.data).assignees : null),
    memberId = Number(identity.member_id) || null;

  async function act(task: Row, action: string, extra: Record<string, unknown> = {}) {
    setBusy(true);
    setError("");
    try {
      await api(
        `/api/tasks/${id(task.id)}`,
        jsonBody({ action, ...extra, expected_version: Number(task.version) }, "PATCH"),
      );
      setNotice("");
      tasks.reload();
    } catch (e) {
      setError(message(e));
      if (e instanceof ApiError && e.status === 409) {
        tasks.reload();
        members.reload();
      }
    } finally {
      setBusy(false);
    }
  }

  function returnTask(task: Row) {
    const reason = window.prompt("退回原因（必填）：", "");
    if (reason === null) return;
    if (!reason.trim()) {
      setError("退回任务必须填写原因");
      return;
    }
    void act(task, "return", { note: reason.trim() });
  }

  return (
    <section>
      <header className="section-header">
        <div>
          <span className="eyebrow">人员分工</span>
          <h2>阅卷任务</h2>
        </div>
        <label className="inline">
          <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />
          只看我的
        </label>
      </header>
      <ErrorBox error={error || tasks.error || members.error} />
      {notice ? <p className="audit-note">{notice}</p> : null}
      <div className="metrics">
        {["待处理", "进行中", "待复核", "已完成"].map((key) => (
          <article className="card" key={key}>
            <span>{key}</span>
            <strong>{text(counts[key]) || "0"}</strong>
          </article>
        ))}
      </div>
      {manager && (
        <form
          className="card"
          onSubmit={async (event) => {
            event.preventDefault();
            const element = event.currentTarget;
            const form = new FormData(element);
            setBusy(true);
            setError("");
            try {
              await api(
                `/api/cases/${caseId}/tasks`,
                jsonBody({
                  title: String(form.get("title") ?? "").trim(),
                  kind: String(form.get("kind") ?? "阅卷"),
                  detail: String(form.get("detail") ?? ""),
                  assignee_member_id: form.get("assignee_member_id")
                    ? Number(form.get("assignee_member_id"))
                    : null,
                  due_at: String(form.get("due_at") ?? "") || null,
                }),
              );
              element.reset();
              setNotice("任务已创建");
              tasks.reload();
            } catch (e) {
              setError(message(e));
              if (e instanceof ApiError && e.status === 409) members.reload();
            } finally {
              setBusy(false);
            }
          }}
        >
          <h3>新建任务</h3>
          <label>
            任务标题
            <input name="title" required minLength={2} maxLength={120} />
          </label>
          <div className="fields">
            <label>
              类型
              <select name="kind" defaultValue="阅卷">
                {KINDS.map((kind) => (
                  <option key={kind}>{kind}</option>
                ))}
              </select>
            </label>
            <label>
              指派给
              <select name="assignee_member_id" defaultValue="">
                <option value="">暂不指派</option>
                {candidates.map((row) => (
                  <option key={text(row.member_id)} value={text(row.member_id)}>
                    {text(row.name)}
                  </option>
                ))}
              </select>
              <small>仅显示可查看并编辑本案的启用成员；请先在案件分工中授权。</small>
            </label>
            <label>
              期限
              <input name="due_at" type="date" />
            </label>
          </div>
          <label>
            说明
            <textarea name="detail" rows={2} maxLength={2000} />
          </label>
          <button className="primary" disabled={busy}>
            创建任务
          </button>
        </form>
      )}
      <div className="card">
        {items.map((task) => {
          const events = list(task.events);
          const lastReturn = [...events].reverse().find((event) => text(event.action) === "return");
          const assignedToMe = memberId !== null && Number(task.assignee_member_id) === memberId;
          const open = ["待处理", "进行中", "待复核"].includes(text(task.status));
          const canManage = flag(task.can_manage, manager);
          const canReviewTask = flag(task.can_review, reviewer);
          const canWorkTask = flag(task.can_work, canWork && (assignedToMe || manager));
          return (
            <article className="row" key={id(task.id)}>
              <div>
                <strong>{text(task.title)}</strong>
                <small>
                  {text(task.kind)} · {text(task.assignee_name) || "未指派"}
                  {task.due_at ? ` · 期限 ${text(task.due_at)}` : ""}
                  {text(task.detail) ? ` · ${text(task.detail)}` : ""}
                </small>
                <small>
                  状态：{text(task.status_label)}
                  {task.overdue ? "（已逾期）" : ""}
                  {lastEventText(task)}
                </small>
                {lastReturn && text(lastReturn.note) ? (
                  <p className="audit-note">最近退回说明：{text(lastReturn.note)}</p>
                ) : null}
                {events.length > 0 ? (
                  <details>
                    <summary>流转记录（{events.length}）</summary>
                    <ol>
                      {events.map((event, index) => (
                        <li key={index}>
                          <strong>{actionLabel(event.action)}</strong> · {text(event.actor)}
                          <small>
                            {text(event.created_at)} · {text(event.from) || "新建"} → {text(event.to)}
                          </small>
                          {text(event.note) ? <p className="audit-note">{text(event.note)}</p> : null}
                        </li>
                      ))}
                    </ol>
                  </details>
                ) : null}
              </div>
              <span>
                {task.overdue ? <span className="badge">逾期</span> : null}
                <span className="badge">{text(task.status_label)}</span>
                {open && canWorkTask && text(task.status) === "待处理" && (
                  <button disabled={busy} onClick={() => void act(task, "start")}>
                    开始
                  </button>
                )}
                {open && canWorkTask && text(task.status) === "进行中" && (
                  <button disabled={busy} onClick={() => void act(task, "submit")}>
                    提交复核
                  </button>
                )}
                {open && canReviewTask && text(task.status) === "待复核" && (
                  <button disabled={busy} onClick={() => void act(task, "complete")}>
                    确认完成
                  </button>
                )}
                {open && canReviewTask && ["进行中", "待复核"].includes(text(task.status)) && (
                  <button disabled={busy} onClick={() => returnTask(task)}>
                    退回
                  </button>
                )}
                {open && canManage && (
                  <button
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm("确认取消该任务？"))
                        void act(task, "cancel", { note: "取消任务" });
                    }}
                  >
                    取消
                  </button>
                )}
              </span>
            </article>
          );
        })}
        {!items.length && <Empty>{mine ? "没有分配给我的未结任务。" : "本案还没有任务。"}</Empty>}
      </div>
    </section>
  );
}
