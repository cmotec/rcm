import type { Problem } from './types';

/** Error carrying an RFC 9457 problem document from the API. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(problem.detail || problem.title);
    this.status = problem.status;
    this.problem = problem;
  }

  get code(): string {
    return this.problem.code;
  }
}

let csrfToken: string | null = null;
let onUnauthenticated: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function setUnauthenticatedHandler(fn: () => void): void {
  onUnauthenticated = fn;
}

function readCsrfCookie(): string | null {
  const m = document.cookie.match(/(?:^|;\s*)(?:__Host-)?csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

export function csrfHeader(): Record<string, string> {
  const token = csrfToken ?? readCsrfCookie();
  return token ? { 'X-CSRF-Token': token } : {};
}

async function toError(resp: Response): Promise<ApiError> {
  let problem: Problem;
  try {
    problem = (await resp.json()) as Problem;
  } catch {
    problem = { type: 'about:blank', title: resp.statusText || 'Error', status: resp.status, code: 'http_error' };
  }
  if (!problem.status) problem.status = resp.status;
  return new ApiError(problem);
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  raw?: boolean;
}

export function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = new URL(path.startsWith('/') ? path : `/api/v1/${path}`, window.location.origin);
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
  }
  return url.pathname + url.search;
}

export async function api<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = (opts.method ?? 'GET').toUpperCase();
  const headers: Record<string, string> = { Accept: 'application/json', ...opts.headers };
  let body: BodyInit | undefined;
  if (opts.body instanceof FormData || opts.body instanceof Blob) {
    body = opts.body;
  } else if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  if (method !== 'GET' && method !== 'HEAD') Object.assign(headers, csrfHeader());
  const resp = await fetch(buildUrl(path, opts.query), {
    method,
    headers,
    body,
    credentials: 'same-origin',
    signal: opts.signal,
  });
  if (!resp.ok) {
    const err = await toError(resp);
    if (resp.status === 401 && onUnauthenticated && !path.includes('/auth/')) onUnauthenticated();
    throw err;
  }
  if (opts.raw) return resp as unknown as T;
  if (resp.status === 204) return undefined as T;
  const type = resp.headers.get('content-type') ?? '';
  return (type.includes('json') ? await resp.json() : await resp.text()) as T;
}

export const get = <T>(path: string, query?: RequestOptions['query']) => api<T>(path, { query });
export const post = <T>(path: string, body?: unknown, query?: RequestOptions['query']) =>
  api<T>(path, { method: 'POST', body, query });
export const patch = <T>(path: string, body?: unknown) => api<T>(path, { method: 'PATCH', body });
export const put = <T>(path: string, body?: unknown) => api<T>(path, { method: 'PUT', body });
export const del = <T>(path: string, query?: RequestOptions['query']) => api<T>(path, { method: 'DELETE', query });

/** WebSocket URL for a path on this origin (ws/wss follows the page scheme). */
export function wsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}${path}`;
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const errors = err.problem.errors as { loc: unknown[]; msg: string }[] | undefined;
    if (errors?.length) return `${err.message}: ${errors.map((e) => `${e.loc.slice(1).join('.')} ${e.msg}`).join('; ')}`;
    return err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
