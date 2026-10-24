import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';
import { Readable } from 'node:stream';

// The upstream is fixed by the operator, never by a browser-controlled URL.
export async function forwardApi(request: Request): Promise<Response> {
  const url = new URL(request.url);
  // Next normalizes Request.url to its listener hostname; retain the browser's
  // Host for FastAPI's Host/Origin check (including its public port).
  const host = request.headers.get('host') || url.host;
  if (!/^[a-zA-Z0-9.[\]:-]+$/.test(host)) return new Response(null, { status: 400 });
  const publicOrigin = process.env.LEXVAULT_PUBLIC_ORIGIN || `${url.protocol}//${host}`;
  const origin = request.headers.get("origin");
  if ((origin && origin !== publicOrigin) || request.headers.get("sec-fetch-site") === "cross-site") {
    return Response.json({ detail: "不允许跨站访问" }, { status: 403 });
  }
  const base = new URL(process.env.LEXVAULT_API_ORIGIN || "http://127.0.0.1:8765");
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.pathname !== '/') {
    return Response.json({ detail: "后端地址配置无效" }, { status: 503 });
  }
  if (!url.pathname.startsWith('/api/')) return new Response(null, { status: 404 });
  const headers = new Headers();
  for (const key of ['accept', 'content-type', 'authorization', 'cookie', 'origin', 'sec-fetch-site']) {
    const value = request.headers.get(key);
    if (value) headers.set(key, value);
  }
  headers.set('host', host);
  // FastAPI deliberately rejects proxies in local mode. Never erase this marker:
  // a local reverse proxy must not turn remote visitors into local administrators.
  headers.set('x-forwarded-host', host);
  const target = new URL(base);
  target.pathname = url.pathname;
  target.search = url.search;
  try {
    // Node fetch replaces Host with the upstream authority. http.request retains
    // the public Host, needed for FastAPI's CSRF checks and OIDC callback URLs.
    return await new Promise<Response>((resolve, reject) => {
      const upstream = (target.protocol === 'https:' ? httpsRequest : httpRequest)(target, {
        method: request.method, headers: Object.fromEntries(headers), signal: request.signal,
      }, (incoming) => {
        const outgoing = new Headers();
        for (const key of ['content-type', 'content-disposition', 'location', 'retry-after', 'x-request-id', 'set-cookie']) {
          const value = incoming.headers[key];
          if (Array.isArray(value)) value.forEach(item => outgoing.append(key, item));
          else if (value) outgoing.set(key, value);
        }
        outgoing.set('cache-control', 'no-store');
        outgoing.set('x-accel-buffering', 'no');
        const status = incoming.statusCode || 502;
        const noBody = request.method === 'HEAD' || [204, 304].includes(status);
        if (noBody) incoming.resume();
        resolve(new Response(noBody ? null : Readable.toWeb(incoming) as ReadableStream<Uint8Array>, { status, headers: outgoing }));
      });
      upstream.on('error', reject);
      if (request.body && !['GET', 'HEAD'].includes(request.method)) {
        const body = Readable.fromWeb(request.body as import('node:stream/web').ReadableStream<Uint8Array>);
        body.on('error', error => upstream.destroy(error));
        body.pipe(upstream);
      } else upstream.end();
    });
  } catch (error) {
    if (request.signal.aborted) throw error;
    return Response.json({ detail: "后端暂时不可用" }, { status: 502 });
  }
}
