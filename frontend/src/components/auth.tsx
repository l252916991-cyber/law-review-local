'use client';
import { useEffect, useState } from 'react';
import { api, jsonBody } from '@/lib/api';
import { message, object, text, type Row } from '@/lib/data';
import { ErrorBox } from './ui';

export function Auth({ children }: { children: (identity: Row, logout: () => Promise<void>) => React.ReactNode }) {
  const [identity, setIdentity] = useState<Row | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  async function check() {
    try { setIdentity(object(await api('/api/auth/me'))); setError(''); } catch (e) { setError(message(e)); }
  }
  useEffect(() => {
    const controller = new AbortController();
    api('/api/auth/me', { signal: controller.signal }).then(value => setIdentity(object(value))).catch(e => { if (!controller.signal.aborted) setError(message(e)); });
    const expired = () => { setIdentity(null); setError('登录已过期，请重新登录'); };
    window.addEventListener('lexvault:unauthorized', expired);
    return () => { controller.abort(); window.removeEventListener('lexvault:unauthorized', expired); };
  }, []);
  async function logout() {
    await api('/api/auth/session', { method: 'DELETE' });
    setIdentity(null); await check();
  }
  if (identity?.authenticated) return children(identity, logout);
  return <main className="login"><div className="brand">律 <span>LexVault</span></div><section className="card"><span className="eyebrow">律师阅卷工作台</span><h1>登录律所工作空间</h1><p>使用获授权的身份查看案件与卷宗。</p>
    <ErrorBox error={error} />
    {!identity && !error ? <p role="status">正在检查登录状态…</p> : <form onSubmit={async event => {
      event.preventDefault(); const form = event.currentTarget; setBusy(true); setError('');
      try { await api('/api/auth/session', jsonBody({ token: new FormData(form).get('token') })); form.reset(); await check(); }
      catch (e) { setError(message(e)); } finally { setBusy(false); }
    }}><label>访问令牌<input type="password" name="token" required autoComplete="off" maxLength={512} /></label><button className="primary" disabled={busy}>{busy ? '正在登录…' : '登录工作空间'}</button></form>}
    {identity?.oidc_enabled ? <form action="/api/auth/oidc/login" method="GET"><button>使用律所账号登录</button></form> : null}
    <button onClick={() => void check()}>重新检查</button><small>{text(identity?.support_contact) || '无法登录时请联系律所管理员。'}</small>
  </section></main>;
}
