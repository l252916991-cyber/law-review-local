"use client";
import { useState } from "react";
import { api, jsonBody } from "@/lib/api";
import { id, message, object, strings, text, type Row } from "@/lib/data";
import { Empty, ErrorBox, Modal, list, useData } from "./ui";

export function Members() {
  const data = useData("/api/members");
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [creating, setCreating] = useState(false),
    [token, setToken] = useState<{ name: string; value: string } | null>(null),
    [busy, setBusy] = useState(false);
  const body = data.data ? object(data.data) : {},
    members = list(body.members),
    configured = list(body.configured),
    roleOptions = list(body.roles),
    permissionOptions = list(body.permissions);

  async function act(path: string, init: RequestInit): Promise<Row | null> {
    setBusy(true);
    setError("");
    try {
      const result = object(await api(path, init));
      data.reload();
      return result;
    } catch (e) {
      setError(message(e));
      return null;
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <header className="section-header">
        <div>
          <span className="eyebrow">律所成员</span>
          <h2>成员与权限</h2>
        </div>
        <button className="primary" disabled={busy} onClick={() => setCreating(true)}>
          添加成员
        </button>
      </header>
      <ErrorBox error={error || data.error} />
      {notice ? <p className="audit-note">{notice}</p> : null}
      {text(body.mode) && text(body.mode) !== "token" ? (
        <p className="audit-note">
          本机模式为单用户；成员管理、令牌与案件分工只在 token 身份模式下生效。
        </p>
      ) : null}
      <p className="audit-note">
        令牌只在创建或轮换时显示一次；停用成员会立即注销其会话。环境变量里配置的令牌仍可登录，
        在这里以“部署配置”身份只读展示。
      </p>
      <div className="card">
        <h3>目录成员</h3>
        {members.map((row) => {
          const roles = list(row.cases)
            .map((item) => `${text(item.title)}（${roleLabel(roleOptions, text(item.role))}）`)
            .join("、");
          return (
            <div className="row" key={id(row.id)}>
              <span>
                {text(row.name)}
                <small>
                  {row.admin ? "管理员 · " : ""}
                  {strings(row.permissions).join("、") || "无权限"}
                  {roles ? ` · ${roles}` : ""}
                </small>
              </span>
              <span>
                <span className="badge">{row.active ? "启用" : "已停用"}</span>
                <button
                  disabled={busy || !row.active}
                  onClick={async () => {
                    const result = await act(`/api/members/${id(row.id)}/tokens`, jsonBody({}));
                    if (result) {
                      setToken({ name: text(row.name), value: text(result.token) });
                      setNotice("旧令牌已失效，请把新令牌交给本人。");
                    }
                  }}
                >
                  轮换令牌
                </button>
                <button
                  disabled={busy}
                  onClick={async () => {
                    if (!window.confirm(row.active ? "停用后该成员将立即失去访问权限，确认停用？" : "确认重新启用？"))
                      return;
                    const result = await act(
                      `/api/members/${id(row.id)}`,
                      jsonBody({ active: !row.active }, "PATCH"),
                    );
                    if (result) setNotice(row.active ? "成员已停用，会话已注销。" : "成员已启用。");
                  }}
                >
                  {row.active ? "停用" : "启用"}
                </button>
              </span>
            </div>
          );
        })}
        {!members.length && <Empty>尚未添加成员。环境变量中的令牌仍可独立登录。</Empty>}
      </div>
      {configured.length > 0 && (
        <div className="card">
          <h3>部署配置身份（只读）</h3>
          {configured.map((row) => (
            <div className="row" key={text(row.name)}>
              <span>
                {text(row.name)}
                <small>
                  {row.admin ? "管理员 · " : ""}
                  {strings(row.permissions).join("、")}
                </small>
              </span>
              <span className="badge">环境配置</span>
            </div>
          ))}
        </div>
      )}
      {creating && (
        <Modal title="添加成员" close={() => setCreating(false)}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              const permissions = form.getAll("permissions").map(String);
              const created = await act(
                "/api/members",
                jsonBody({
                  name: String(form.get("name") ?? "").trim(),
                  admin: form.get("admin") === "on",
                  permissions,
                }),
              );
              if (!created) return;
              setCreating(false);
              setToken({ name: text(object(created.member).name), value: text(created.token) });
              setNotice("成员已创建；请立即把令牌交给本人，之后无法再次查看。");
            }}
          >
            <label>
              姓名 / 登录标识
              <input name="name" required maxLength={60} />
            </label>
            <fieldset>
              <legend>权限</legend>
              {permissionOptions.map((item) => (
                <label key={text(item.key)} className="inline">
                  <input type="checkbox" name="permissions" value={text(item.key)} defaultChecked />
                  {text(item.label)}
                </label>
              ))}
            </fieldset>
            <label className="inline">
              <input type="checkbox" name="admin" />
              管理员（可管理成员与全部案件）
            </label>
            <p className="audit-note">
              案件分工在案件页“案件分工”中设置；角色会限制该成员在本案能做什么。
            </p>
            <ErrorBox error={error} />
            <button className="primary">创建成员并生成令牌</button>
          </form>
        </Modal>
      )}
      {token && (
        <Modal title="访问令牌（只显示一次）" close={() => setToken(null)}>
          <p>
            成员 <strong>{token.name}</strong> 的访问令牌：
          </p>
          <pre className="token">{token.value}</pre>
          <p className="audit-note">
            请通过可靠渠道交给本人，不要在聊天群或邮件正文中长期留存；需要时可直接轮换。
          </p>
          <button className="primary" onClick={() => setToken(null)}>
            我已保存
          </button>
        </Modal>
      )}
    </section>
  );
}

export function CaseAssignment({ caseId }: { caseId: number }) {
  const data = useData(`/api/cases/${caseId}/members`);
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const body = data.data ? object(data.data) : {},
    assignments = list(body.assignments),
    candidates = list(body.candidates),
    roles = list(body.roles);

  async function act(path: string, init: RequestInit) {
    setBusy(true);
    setError("");
    try {
      await api(path, init);
      data.reload();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <header>
        <h2>案件分工</h2>
        <span className="badge">{assignments.length} 人</span>
      </header>
      <ErrorBox error={error || data.error} />
      <p className="audit-note">
        角色限定该成员在本案能做什么：承办律师可编辑、确认与导出；复核律师只能确认与导出；助理可编辑不能确认；只读仅查看。
      </p>
      {assignments.map((row) => (
        <div className="row" key={text(row.member_id)}>
          <span>
            {text(row.name)}
            <small>{text(row.role_label)}</small>
          </span>
          <span>
            <select
              aria-label={`${text(row.name)} 的角色`}
              value={text(row.role)}
              disabled={busy}
              onChange={(event) =>
                void act(`/api/cases/${caseId}/members/${row.member_id}`, jsonBody({ role: event.target.value }, "PUT"))
              }
            >
              {roles.map((role) => (
                <option key={text(role.key)} value={text(role.key)}>
                  {text(role.label)}
                </option>
              ))}
            </select>
            <button
              disabled={busy}
              onClick={() => {
                if (window.confirm(`确认把 ${text(row.name)} 移出本案？`))
                  void act(`/api/cases/${caseId}/members/${row.member_id}`, { method: "DELETE" });
              }}
            >
              移出
            </button>
          </span>
        </div>
      ))}
      {!assignments.length && <Empty>本案还没有分配成员；仅有管理员可访问。</Empty>}
      {candidates.length > 0 && (
        <form
          className="fields"
          onSubmit={async (event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            await act(`/api/cases/${caseId}/members/${form.get("member_id")}`, jsonBody({ role: form.get("role") }, "PUT"));
          }}
        >
          <label>
            添加成员
            <select name="member_id" required>
              {candidates.map((row) => (
                <option key={text(row.member_id)} value={text(row.member_id)}>
                  {text(row.name)}
                </option>
              ))}
            </select>
          </label>
          <label>
            角色
            <select name="role" defaultValue="assistant">
              {roles.map((role) => (
                <option key={text(role.key)} value={text(role.key)}>
                  {text(role.label)}
                </option>
              ))}
            </select>
          </label>
          <button className="primary" disabled={busy}>
            分配
          </button>
        </form>
      )}
    </section>
  );
}

function roleLabel(roles: Row[], key: string) {
  return text(roles.find((row) => text(row.key) === key)?.label) || key;
}
