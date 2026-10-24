import type { paths } from './schema';

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) { super(message); }
}

export async function api<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  if (!path.startsWith('/api/')) throw new Error('API path must be local');
  const response = await fetch(path, { ...options, credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail: unknown = body.detail;
    if (response.status === 401 && typeof window !== 'undefined') window.dispatchEvent(new Event('lexvault:unauthorized'));
    throw new ApiError(response.status, typeof detail === 'string' ? detail : `请求失败 (${response.status})`, detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function jsonBody(value: unknown, method = 'POST'): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) };
}

// Template paths and payloads come from FastAPI, including its validation rules.
export type ApiPath = keyof paths;
export type ChatRequest = NonNullable<paths['/api/cases/{case_id}/chat/stream']['post']['requestBody']>['content']['application/json'];
