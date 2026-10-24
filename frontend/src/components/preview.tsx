'use client';
import { object, text } from '@/lib/data';
import { ErrorBox, useData } from './ui';

export type PageTarget = { document: number; page: number };
export function Preview({ target, close, select }: { target: PageTarget; close: () => void; select: (target: PageTarget) => void }) {
  const { data, error } = useData(`/api/documents/${target.document}/pages/${target.page}`);
  const page = data ? object(data) : {};
  return <aside className="preview" aria-label="卷宗原文"><header><div><span className="eyebrow">原页溯源</span><h2>{text(page.name) || '卷宗原文'}</h2></div><button onClick={close} aria-label="关闭原文">×</button></header><nav><button disabled={target.page <= 1} onClick={() => select({ ...target, page: target.page - 1 })}>上一页</button><span>第 {target.page} 页</span><button onClick={() => select({ ...target, page: target.page + 1 })}>下一页</button></nav><ErrorBox error={error} /><pre>{data ? text(page.text) : error ? '' : '正在加载原文…'}</pre><a href={`/api/documents/${target.document}/file`} target="_blank" rel="noreferrer">下载源文件</a></aside>;
}
