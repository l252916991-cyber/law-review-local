import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer, type RequestListener } from 'node:http';
import { once } from 'node:events';
import { forwardApi } from '../src/lib/proxy.ts';

async function server(handler: RequestListener) {
  const instance = createServer(handler).listen(0, '127.0.0.1');
  await once(instance, 'listening');
  const address = instance.address();
  if (!address || typeof address === 'string') throw new Error('No port');
  return { instance, origin: `http://127.0.0.1:${address.port}` };
}
test('cross-site calls are denied before reaching FastAPI', async () => {
  const response = await forwardApi(new Request('http://localhost:3000/api/cases', { headers: { origin: 'https://attacker.invalid' } }));
  assert.equal(response.status, 403);
});
test('public Host, Origin and credentials survive real upstream HTTP', async (t) => {
  const upstream = await server((request, response) => {
    assert.equal(request.headers.host, '127.0.0.1:3210');
    assert.equal(request.headers.origin, 'http://127.0.0.1:3210');
    assert.equal(request.headers['x-forwarded-host'], '127.0.0.1:3210');
    assert.equal(request.headers.cookie, 'lexvault_session=test');
    response.setHeader('set-cookie', 'lexvault_session=new; HttpOnly');
    response.end('ok');
  });
  const previous = process.env.LEXVAULT_API_ORIGIN;
  process.env.LEXVAULT_API_ORIGIN = upstream.origin;
  t.after(() => {
    if (previous === undefined) delete process.env.LEXVAULT_API_ORIGIN;
    else process.env.LEXVAULT_API_ORIGIN = previous;
    upstream.instance.closeAllConnections(); upstream.instance.close();
  });
  const response = await forwardApi(new Request('http://localhost:3210/api/auth/session', {
    method: 'POST', headers: { host: '127.0.0.1:3210', origin: 'http://127.0.0.1:3210', cookie: 'lexvault_session=test' },
  }));
  assert.equal(response.status, 200);
  assert.match(response.headers.get('set-cookie')!, /HttpOnly/);
  assert.equal(await response.text(), 'ok');
});
test('stream first frame arrives before completion and cancellation closes upstream', async (t) => {
  let closed!: () => void;
  const closure = new Promise<void>(resolve => { closed = resolve; });
  const upstream = await server((_request, response) => {
    response.setHeader('content-type', 'text/event-stream');
    response.write('event:status\ndata:{}\n\n');
    response.on('close', closed);
  });
  const previous = process.env.LEXVAULT_API_ORIGIN;
  process.env.LEXVAULT_API_ORIGIN = upstream.origin;
  t.after(() => {
    if (previous === undefined) delete process.env.LEXVAULT_API_ORIGIN;
    else process.env.LEXVAULT_API_ORIGIN = previous;
    upstream.instance.closeAllConnections(); upstream.instance.close();
  });
  const abort = new AbortController();
  const response = await forwardApi(new Request('http://localhost:3000/api/cases/1/chat/stream', { signal: abort.signal }));
  assert.equal(response.headers.get('cache-control'), 'no-store');
  const reader = response.body!.getReader();
  assert.match(new TextDecoder().decode((await reader.read()).value), /event:status/);
  abort.abort();
  await closure;
  await reader.cancel().catch(() => {});
});
