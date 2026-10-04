/**
 * Typed JSON client. Errors are normalised to Splash's OpenAI-shaped body
 * `{error: {message, type, code}}` (splash/server/server.py `_error`), and the
 * Anthropic shape `{type: "error", error: {type, message}}` and FastAPI's
 * `{detail: ...}` are mapped onto it.
 */

/** One validation problem from the manager (`IssueOut`: path, key, message, ...). */
export interface ErrorIssue {
  path?: Array<string | number>;
  key?: string;
  message: string;
  [extra: string]: unknown;
}

export interface ErrorBody {
  message: string;
  type: string;
  code: string | null;
  /** Manager extension: field-level issues for 422 invalid_settings. */
  issues?: ErrorIssue[];
  /** Manager extension: structured context (e.g. a memory budget). */
  details?: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly code: string | null;
  readonly issues: ErrorIssue[];
  readonly details: Record<string, unknown> | null;
  readonly body: unknown;
  /** Seconds from a `Retry-After` header (503s from the manager and Splash), or null. */
  readonly retryAfter: number | null;

  constructor(status: number, error: ErrorBody, body?: unknown, retryAfter: number | null = null) {
    super(error.message);
    this.retryAfter = retryAfter;
    this.name = 'ApiError';
    this.status = status;
    this.type = error.type;
    this.code = error.code;
    this.issues = error.issues ?? [];
    this.details = error.details ?? null;
    this.body = body;
  }
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);

function statusType(status: number): string {
  if (status === 401) return 'authentication_error';
  if (status === 403) return 'permission_error';
  if (status === 404) return 'not_found_error';
  if (status === 409) return 'conflict_error';
  if (status === 422 || (status >= 400 && status < 500)) return 'invalid_request_error';
  if (status === 501) return 'not_implemented';
  if (status === 503) return 'overloaded_error';
  if (status === 507) return 'insufficient_storage';
  return 'server_error';
}

/** Extracts an ErrorBody from any of the known error shapes, or null. */
export function parseErrorBody(body: unknown, status = 0): ErrorBody | null {
  if (!isRecord(body)) return null;
  const err = body.error;
  if (isRecord(err)) {
    const out: ErrorBody = {
      message: str(err.message) ?? 'Request failed',
      type: str(err.type) ?? statusType(status),
      code: str(err.code),
    };
    if (Array.isArray(err.issues)) {
      out.issues = err.issues
        .filter(isRecord)
        .map((i) => ({ ...i, message: str(i.message) ?? str(i.msg) ?? 'Invalid value' }));
    }
    if (isRecord(err.details)) out.details = err.details;
    return out;
  }
  if (typeof err === 'string') return { message: err, type: statusType(status), code: null };
  const detail = body.detail;
  if (typeof detail === 'string') return { message: detail, type: statusType(status), code: null };
  if (isRecord(detail)) return parseErrorBody({ error: detail }, status);
  if (Array.isArray(detail) && detail.length > 0) {
    const messages = detail.map((d) => {
      if (!isRecord(d)) return String(d);
      const loc = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body').join('.') : '';
      return loc ? `${loc}: ${String(d.msg ?? '')}` : String(d.msg ?? '');
    });
    return { message: messages.join('; '), type: 'invalid_request_error', code: 'validation_error' };
  }
  return null;
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  query?: Query;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Return the raw Response instead of parsing JSON. */
  raw?: boolean;
}

type UnauthorizedHandler = (error: ApiError) => void;
let onUnauthorized: UnauthorizedHandler | null = null;

/** Registers a handler for 401s from /api/admin (used to route to /admin/login). */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

export function buildUrl(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.append(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}${path.includes('?') ? '&' : '?'}${qs}` : path;
}

export async function errorFromResponse(res: Response): Promise<ApiError> {
  let body: unknown = null;
  const text = await res.text().catch(() => '');
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  const parsed = parseErrorBody(body, res.status) ?? {
    message: (typeof body === 'string' && body.trim()) || `${res.status} ${res.statusText || 'Request failed'}`,
    type: statusType(res.status),
    code: null,
  };
  const ra = Number(res.headers?.get?.('Retry-After') ?? NaN);
  return new ApiError(res.status, parsed, body, Number.isFinite(ra) && ra >= 0 ? ra : null);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = options.body === undefined ? 'GET' : 'POST', body, query, signal, raw } = options;
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers };
  const init: RequestInit = { method, headers, credentials: 'same-origin' };
  if (signal) init.signal = signal;
  if (body !== undefined) {
    if (body instanceof FormData || body instanceof Blob || typeof body === 'string') {
      init.body = body;
    } else {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
  }
  let res: Response;
  try {
    res = await fetch(buildUrl(path, query), init);
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError(0, { message: 'Cannot reach Splash GUI. Is the manager running?', type: 'network_error', code: 'network' }, cause);
  }
  if (!res.ok) {
    const error = await errorFromResponse(res);
    if (res.status === 401 && path.startsWith('/api/admin') && onUnauthorized) onUnauthorized(error);
    throw error;
  }
  if (raw) return res as unknown as T;
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    return text as unknown as T;
  }
}

/** Shorthands for the manager admin API (SPEC §14), rooted at /api/admin. */
export const ADMIN = '/api/admin';

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>(`${ADMIN}${path}`, { method: 'GET', ...(query ? { query } : {}), ...(signal ? { signal } : {}) }),
  post: <T>(path: string, body?: unknown, signal?: AbortSignal) =>
    request<T>(`${ADMIN}${path}`, { method: 'POST', body: body ?? {}, ...(signal ? { signal } : {}) }),
  put: <T>(path: string, body: unknown) => request<T>(`${ADMIN}${path}`, { method: 'PUT', body }),
  del: <T>(path: string, query?: Query) => request<T>(`${ADMIN}${path}`, { method: 'DELETE', ...(query ? { query } : {}) }),
};
