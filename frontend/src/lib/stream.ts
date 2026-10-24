export type Data = Record<string, unknown>;
export type StreamEvent =
  | { type: 'status'; data: Data & { phase: string } }
  | { type: 'token'; data: Data & { text: string; provisional: true } }
  | { type: 'citations'; data: Data & { citations: unknown[] } }
  | { type: 'final'; data: Data & { answer: string } }
  | { type: 'retract'; data: Data & { message: string } }
  | { type: 'done'; data: Data & { status: 'ok' | 'failed' | 'cancelled' } };

export function parseEvent(frame: string): StreamEvent | null {
  let type = 'message';
  const lines: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const key = line.slice(0, colon);
    const value = line.slice(colon + 1).replace(/^ /, '');
    if (key === 'event') type = value;
    if (key === 'data') lines.push(value);
  }
  if (!['status', 'token', 'citations', 'final', 'retract', 'done'].includes(type)) return null;
  const data: unknown = JSON.parse(lines.join('\n'));
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('流式数据格式无效');
  const d = data as Data;
  const valid = type === 'status' ? typeof d.phase === 'string'
    : type === 'token' ? typeof d.text === 'string' && d.provisional === true
    : type === 'citations' ? Array.isArray(d.citations)
    : type === 'final' ? typeof d.answer === 'string'
    : type === 'retract' ? typeof d.message === 'string'
    : ['ok', 'failed', 'cancelled'].includes(String(d.status));
  if (!valid) throw new Error(`流式事件 ${type} 格式无效`);
  return { type, data: d } as StreamEvent;
}

export async function consumeStream(body: ReadableStream<Uint8Array>, onEvent: (event: StreamEvent) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', sawStatus = false, sawFinal = false, retracted = false, finished = false;
  function dispatch(frame: string) {
    const event = parseEvent(frame);
    if (!event) return;
    if (finished) throw new Error('结束后收到额外事件');
    if (event.type === 'status') sawStatus = true;
    else if (!sawStatus) throw new Error('流式事件缺少初始状态');
    if (event.type === 'token' && (sawFinal || retracted)) throw new Error('终态后收到草稿');
    if (event.type === 'final') {
      if (retracted) throw new Error('撤回后收到答案');
      sawFinal = true;
    }
    if (event.type === 'retract') retracted = true;
    if (event.type === 'done') {
      if (event.data.status === 'ok' && (!sawFinal || retracted)) throw new Error('答案未完成');
      finished = true;
    }
    onEvent(event);
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (buffer.length > 2_000_000) throw new Error('流式帧超过大小上限');
      let match: RegExpExecArray | null;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        dispatch(buffer.slice(0, match.index));
        buffer = buffer.slice(match.index + match[0].length);
      }
      if (done) break;
    }
    if (buffer.trim()) dispatch(buffer);
    if (!finished) throw new Error('连接中断，答案尚未确认');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
