"use client";
import { useState } from "react";
import { api, jsonBody } from "@/lib/api";
import { id, message, object, strings, text, type Row } from "@/lib/data";
import { Empty, ErrorBox, Modal, list, useData } from "./ui";

/**
 * Conflict checks and information walls for one case.
 *
 * The backend decides everything that matters (matching, who may decide, what a
 * viewer may see of another case, which writes are blocked); this view shows
 * it and collects the inputs.
 */
const ROLE_LABELS: Record<string, string> = { client: "委托人", opposing: "对方当事人", related: "关联方" };
const KIND_LABELS: Record<string, string> = { direct: "直接冲突", related: "关联冲突" };
const MATCH_LABELS: Record<string, string> = {
  name: "名称相同",
  former_name: "曾用名相同",
  credit_code: "统一社会信用代码相同",
};
const STATUS_NOTES: Record<string, string> = {
  unchecked: "尚未登记当事人。登记后系统会检索全所案件，包括已归档和设有隔离墙的案件。",
  clear: "未发现冲突，可以正常开展工作。",
  pending: "检查命中。合伙人放行前，本案不能上传材料、问答或修改证据；查看不受影响。",
  approved: "合伙人已写明理由放行，可以正常开展工作。",
  rejected: "未获放行，本案不能开展工作。改正当事人后会重新检查。",
};

export function conflictBlocked(status: string) {
  return status === "pending" || status === "rejected";
}

function statusClass(status: string) {
  return status === "pending" ? "badge warn" : status === "rejected" ? "badge danger" : "badge";
}

/** Names typed one per line, or separated by 、，, ; — the same split everywhere. */
function names(value: FormDataEntryValue | null) {
  return String(value ?? "")
    .split(/[\n、，,;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

type PartyDraft = { role: string; name: string; former_names: string; credit_code: string };

export function Compliance({
  caseId,
  identity,
  conflict,
  reloadConflict,
  hidden,
}: {
  caseId: number;
  identity: Row;
  conflict: { data?: unknown; error?: string };
  reloadConflict: () => void;
  hidden: () => void;
}) {
  const body = conflict.data ? object(conflict.data) : null;
  const status = text(body?.status) || "unchecked";
  const check = body?.check ? object(body.check) : null;
  const parties = list(body?.parties);
  const [editing, setEditing] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const admin = Boolean(identity.admin);
  const ownCheck = text(identity.mode) === "token" && check !== null && text(check.requested_by) === text(identity.name);

  async function act(path: string, init: RequestInit, done: (result: Row) => void) {
    setBusy(true);
    setError("");
    try {
      done(object(await api(path, init)));
      reloadConflict();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <header className="section-header">
        <div>
          <span className="eyebrow">受理前审查</span>
          <h2>利益冲突与隔离</h2>
        </div>
        <span className={statusClass(status)}>{text(body?.status_label) || "未检查"}</span>
      </header>
      <ErrorBox error={error || conflict.error} />
      {notice ? <p role="status" className="audit-note">{notice}</p> : null}
      <section className="card">
        <header>
          <h3>当事人</h3>
          <span className="actions">
            <button disabled={busy} onClick={() => setEditing(true)}>
              {parties.length ? "修改当事人" : "登记当事人"}
            </button>
            {parties.length > 0 && (
              <button
                disabled={busy}
                onClick={() =>
                  act(`/api/cases/${caseId}/conflict-check`, jsonBody({}), (result) =>
                    setNotice(`已重新检查：${text(result.status_label)}`),
                  )
                }
              >
                重新检查
              </button>
            )}
          </span>
        </header>
        <p className="audit-note">{STATUS_NOTES[status] ?? ""}</p>
        {parties.map((party) => (
          <div className="row" key={id(party.id)}>
            <span>
              {text(party.name)}
              <small>
                {ROLE_LABELS[text(party.role)] ?? text(party.role)}
                {strings(party.former_names).length ? ` · 曾用名：${strings(party.former_names).join("、")}` : ""}
                {text(party.credit_code) ? ` · ${text(party.credit_code)}` : ""}
              </small>
            </span>
          </div>
        ))}
        {!parties.length && <Empty>尚未登记当事人</Empty>}
      </section>
      {check && (
        <section className="card">
          <header>
            <h3>最近一次检查</h3>
            <small>
              {text(check.requested_by)} · {text(check.requested_at)}
            </small>
          </header>
          {list(check.hits).map((hit, index) => (
            <div className="row hit" key={index}>
              <span>
                <span className={hit.kind === "direct" ? "badge danger" : "badge warn"}>
                  {KIND_LABELS[text(hit.kind)] ?? text(hit.kind)}
                </span>{" "}
                本方{ROLE_LABELS[text(object(hit.our_party).role)] ?? ""}「{text(object(hit.our_party).name)}」
                <small>
                  {MATCH_LABELS[text(hit.matched_on)] ?? text(hit.matched_on)} ·{" "}
                  {hit.restricted
                    ? "命中本所另一案件；你无权查看是哪个案件，请联系风控合伙人"
                    : `案件 #${text(hit.case_id)}《${text(hit.case_title)}》${text(hit.case_no) ? ` ${text(hit.case_no)}` : ""}中的${
                        ROLE_LABELS[text(object(hit.their_party).role)] ?? ""
                      }「${text(object(hit.their_party).name)}」`}
                </small>
              </span>
            </div>
          ))}
          {!list(check.hits).length && <Empty>未命中其他案件</Empty>}
          {text(check.decided_by) && (
            <p className="audit-note">
              {text(check.decided_by)} 于 {text(check.decided_at)}
              {status === "approved" ? " 放行" : " 驳回"}：{text(check.decision_reason)}
            </p>
          )}
          {status === "pending" &&
            (admin ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  const submitter = (e.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
                  const decision = submitter?.value === "reject" ? "reject" : "approve";
                  const reason = String(new FormData(e.currentTarget).get("reason") ?? "").trim();
                  void act(
                    `/api/cases/${caseId}/conflict-check/decision`,
                    jsonBody({ decision, reason }),
                    (result) => setNotice(`已${decision === "approve" ? "放行" : "驳回"}：${text(result.status_label)}`),
                  );
                }}
              >
                <label>
                  决定理由
                  <textarea
                    name="reason"
                    required
                    minLength={10}
                    maxLength={500}
                    placeholder="例如：已取得双方书面同意，并对承办团队设立隔离墙"
                    disabled={ownCheck}
                  />
                </label>
                {ownCheck && <p className="audit-note">这次检查由你发起，需由另一位合伙人决定。</p>}
                <button className="primary" name="decision" value="approve" disabled={busy || ownCheck}>
                  放行
                </button>
                <button name="decision" value="reject" disabled={busy || ownCheck}>
                  驳回
                </button>
              </form>
            ) : (
              <p className="audit-note">需要具有管理权限的合伙人决定是否放行。</p>
            ))}
        </section>
      )}
      {admin && <WallPanel caseId={caseId} identity={identity} hidden={hidden} />}
      {editing && (
        <PartiesEditor
          initial={parties.map((party) => ({
            role: text(party.role),
            name: text(party.name),
            former_names: strings(party.former_names).join("、"),
            credit_code: text(party.credit_code),
          }))}
          busy={busy}
          error={error}
          close={() => setEditing(false)}
          save={(drafts) =>
            act(
              `/api/cases/${caseId}/parties`,
              jsonBody(
                {
                  parties: drafts.map((draft) => ({
                    role: draft.role,
                    name: draft.name.trim(),
                    former_names: names(draft.former_names),
                    credit_code: draft.credit_code.trim() || null,
                  })),
                },
                "PUT",
              ),
              (result) => {
                setEditing(false);
                setNotice(`已保存并检查：${text(result.status_label)}`);
              },
            )
          }
        />
      )}
    </section>
  );
}

function PartiesEditor({
  initial,
  busy,
  error,
  close,
  save,
}: {
  initial: PartyDraft[];
  busy: boolean;
  error: string;
  close: () => void;
  save: (drafts: PartyDraft[]) => void;
}) {
  const [drafts, setDrafts] = useState<PartyDraft[]>(
    initial.length ? initial : [{ role: "client", name: "", former_names: "", credit_code: "" }],
  );
  const update = (index: number, change: Partial<PartyDraft>) =>
    setDrafts((current) => current.map((draft, at) => (at === index ? { ...draft, ...change } : draft)));
  return (
    <Modal title="登记当事人" close={close}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save(drafts.filter((draft) => draft.name.trim()));
        }}
      >
        <p className="audit-note">保存后立即检索全所案件。曾用名可填多个，用顿号或逗号分隔。</p>
        {drafts.map((draft, index) => (
          <fieldset className="fields party" key={index}>
            <legend>当事人 {index + 1}</legend>
            <label>
              身份
              <select value={draft.role} onChange={(e) => update(index, { role: e.target.value })}>
                {Object.entries(ROLE_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              名称
              <input
                value={draft.name}
                maxLength={120}
                onChange={(e) => update(index, { name: e.target.value })}
              />
            </label>
            <label>
              曾用名
              <input value={draft.former_names} onChange={(e) => update(index, { former_names: e.target.value })} />
            </label>
            <label>
              统一社会信用代码
              <input
                value={draft.credit_code}
                maxLength={18}
                onChange={(e) => update(index, { credit_code: e.target.value })}
              />
            </label>
            <button
              type="button"
              aria-label={`删除当事人 ${index + 1}`}
              onClick={() => setDrafts((current) => current.filter((_, at) => at !== index))}
            >
              删除
            </button>
          </fieldset>
        ))}
        <button
          type="button"
          onClick={() =>
            setDrafts((current) => [...current, { role: "opposing", name: "", former_names: "", credit_code: "" }])
          }
        >
          ＋ 添加当事人
        </button>
        <ErrorBox error={error} />
        <button className="primary" disabled={busy}>
          保存并检查
        </button>
      </form>
    </Modal>
  );
}

function WallPanel({ caseId, identity, hidden }: { caseId: number; identity: Row; hidden: () => void }) {
  const walls = useData("/api/case-walls");
  const wall = list(walls.data).find((row) => row.case_id === caseId) ?? null;
  const [editing, setEditing] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  if (text(identity.mode) === "local")
    return (
      <section className="card">
        <h3>信息隔离墙</h3>
        <p className="audit-note">本机单人模式只有一个身份，分不出墙内墙外，不能设墙。</p>
      </section>
    );
  return (
    <section className="card">
      <header>
        <h3>信息隔离墙</h3>
        <button disabled={busy} onClick={() => setEditing(true)}>
          {wall ? "调整隔离墙" : "设立隔离墙"}
        </button>
      </header>
      <ErrorBox error={error || walls.error} />
      {wall ? (
        <>
          <p>
            墙内名单：{strings(wall.insiders).join("、")}
            <small> · 理由：{text(wall.reason)}</small>
          </p>
          <p className="audit-note">名单以外的人（包括管理员）打开本案都会得到“案件不存在”。</p>
        </>
      ) : (
        <p className="audit-note">未设墙。设墙后只有名单上的人能看到本案，管理员也不例外。</p>
      )}
      {editing && (
        <Modal title={wall ? "调整隔离墙" : "设立隔离墙"} close={() => setEditing(false)}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              const insiders = names(form.get("insiders"));
              const outside = !insiders.includes(text(identity.name));
              if (outside && !window.confirm("你不在墙内名单上，设立后本案会从你的案件列表中消失。确认？")) return;
              setBusy(true);
              setError("");
              try {
                await api(
                  `/api/case-walls/${caseId}`,
                  jsonBody({ reason: String(form.get("reason") ?? "").trim(), insiders }, "PUT"),
                );
                setEditing(false);
                if (outside) hidden();
                else walls.reload();
              } catch (err) {
                setError(message(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label>
              设墙理由
              <textarea name="reason" required minLength={4} maxLength={500} defaultValue={text(wall?.reason)} />
            </label>
            <label>
              墙内名单
              <textarea
                name="insiders"
                required
                defaultValue={strings(wall?.insiders).join("\n")}
                placeholder="每行一个，须与登录身份的名称一致"
              />
            </label>
            <ErrorBox error={error} />
            <button className="primary" disabled={busy}>
              保存
            </button>
          </form>
        </Modal>
      )}
    </section>
  );
}

/** Every wall in the firm, for administrators: titles stay hidden, as the API keeps them out. */
export function Walls({ identity, close, changed }: { identity: Row; close: () => void; changed: () => void }) {
  const walls = useData("/api/case-walls");
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [action, setAction] = useState<{ kind: "break-glass" | "remove"; caseId: number } | null>(null),
    [busy, setBusy] = useState(false);
  const rows = list(walls.data);
  return (
    <Modal title="信息隔离墙" close={close}>
      <p className="audit-note">
        这里只显示案件编号，不显示标题：管理隔离墙的人自己可能在墙外。破窗访问须写明理由，有效 30 分钟，全程留痕。
      </p>
      <ErrorBox error={error || walls.error} />
      {notice ? <p role="status" className="audit-note">{notice}</p> : null}
      {rows.map((wall) => {
        const caseId = id(wall.case_id);
        const inside = strings(wall.insiders).includes(text(identity.name));
        const grant = list(wall.grants).find(
          (row) => text(row.principal) === text(identity.name) && Number(row.expires_at) * 1000 > Date.now(),
        );
        return (
          <article className="row" key={caseId}>
            <span>
              案件 #{caseId}
              <small>
                {text(wall.reason)} · 墙内：{strings(wall.insiders).join("、")}
                {grant ? ` · 你的破窗访问有效至 ${new Date(Number(grant.expires_at) * 1000).toLocaleTimeString()}` : ""}
              </small>
            </span>
            <span className="actions">
              {!inside && !grant && (
                <button disabled={busy} onClick={() => setAction({ kind: "break-glass", caseId })}>
                  破窗访问
                </button>
              )}
              <button disabled={busy} onClick={() => setAction({ kind: "remove", caseId })}>
                撤除
              </button>
            </span>
          </article>
        );
      })}
      {!rows.length && <Empty>当前没有设立隔离墙的案件</Empty>}
      {action && (
        <form
          aria-label={action.kind === "remove" ? "撤除隔离墙" : "破窗访问"}
          onSubmit={async (e) => {
            e.preventDefault();
            const reason = String(new FormData(e.currentTarget).get("reason") ?? "").trim();
            setBusy(true);
            setError("");
            try {
              if (action.kind === "remove")
                await api(`/api/case-walls/${action.caseId}?reason=${encodeURIComponent(reason)}`, { method: "DELETE" });
              else await api(`/api/case-walls/${action.caseId}/break-glass`, jsonBody({ reason }));
              setNotice(
                action.kind === "remove"
                  ? `案件 #${action.caseId} 的隔离墙已撤除`
                  : `已获得案件 #${action.caseId} 的 30 分钟访问权限，已记入审计`,
              );
              setAction(null);
              walls.reload();
              changed();
            } catch (err) {
              setError(message(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            {action.kind === "remove" ? `撤除案件 #${action.caseId} 隔离墙的理由` : `破窗访问案件 #${action.caseId} 的理由`}
            <textarea name="reason" required minLength={action.kind === "remove" ? 4 : 10} maxLength={500} />
          </label>
          <button className="primary" disabled={busy}>
            确认
          </button>
          <button type="button" onClick={() => setAction(null)}>
            取消
          </button>
        </form>
      )}
    </Modal>
  );
}
