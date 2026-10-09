/**
 * fetch-based SSE reader for POST streams such as /v1/chat/completions with
 * `stream: true`. Splash frames events as `data: <json>\n\n` (OpenAI shapes) or
 * `event: <name>\ndata: <json>\n\n` (Anthropic / Responses shapes), sends
 * `: splash-keepalive` comments, ends OpenAI streams with `data: [DONE]`, and reports
 * mid-stream failures as a data payload `{error: {message, type, code}}`
 * (splash/server/server.py `_sse`, `_sse_error`, `_sse_keepalive`).
 */

import { ApiError, errorFromResponse, parseErrorBody } from './client';

export interface SseEvent {
  /** The `event:` field, or "message" when absent. */
  event: string;
  /** Raw `data:` payload (multi-line data joined with "\n"). */
  data: string;
  id?: string;
}

/** Incremental SSE parser (WHATWG event-stream rules). Feed text chunks, get whole events. */
export class SseParser {
  private buffer = '';
  private event = '';
  private data: string[] = [];
  private id: string | undefined;

  push(chunk: string): SseEvent[] {
    this.buffer += chunk;
    const out: SseEvent[] = [];
    let newline: number;
    while ((newline = this.nextLineEnd()) >= 0) {
      let line = this.buffer.slice(0, newline);
      const skip = this.buffer[newline] === '\r' && this.buffer[newline + 1] === '\n' ? 2 : 1;
      this.buffer = this.buffer.slice(newline + skip);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      const evt = this.line(line);
      if (evt) out.push(evt);
    }
    return out;
  }

  /** Flushes a trailing event that was not followed by a blank line. */
  end(): SseEvent[] {
    const out = this.buffer ? this.push('\n') : [];
    const last = this.line('');
    return last ? [...out, last] : out;
  }

  private nextLineEnd(): number {
    const n = this.buffer.indexOf('\n');
    const r = this.buffer.indexOf('\r');
    if (r >= 0 && (n < 0 || r < n)) {
      // A lone CR at the end of the buffer may be the first half of CRLF.
      if (r === this.buffer.length - 1) return -1;
      return r;
    }
    return n;
  }

  private line(line: string): SseEvent | null {
    if (line === '') {
      if (this.data.length === 0) {
        this.event = '';
        return null;
      }
      const evt: SseEvent = { event: this.event || 'message', data: this.data.join('\n') };
      if (this.id !== undefined) evt.id = this.id;
      this.event = '';
      this.data = [];
      return evt;
    }
    if (line.startsWith(':')) return null;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.event = value;
    else if (field === 'data') this.data.push(value);
    else if (field === 'id' && !value.includes('\0')) this.id = value;
    return null;
  }
}

/** Yields raw SSE events from a streaming Response body. */
export async function* readSse(res: Response, signal?: AbortSignal): AsyncGenerator<SseEvent> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  const onAbort = () => void reader.cancel().catch(() => undefined);
  signal?.addEventListener('abort', onAbort, { once: true });
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const evt of parser.push(decoder.decode(value, { stream: true }))) yield evt;
    }
    for (const evt of parser.push(decoder.decode())) yield evt;
    for (const evt of parser.end()) yield evt;
  } finally {
    signal?.removeEventListener('abort', onAbort);
    reader.releaseLock();
  }
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

/**
 * The error body carried by a stream event, or null. Splash sends
 * `data: {"error": {...}}` on chat/completions streams, `event: error` with
 * `{"error": {...}}` on Messages streams, and `event: response.failed` with
 * `response.error` on Responses streams (splash/server/server.py `_sse_error`,
 * `_anthropic_stream`, `_responses_stream`).
 */
export function streamError(event: string, data: unknown): unknown {
  if (event === 'response.failed') {
    const response = isRecord(data) && isRecord(data.response) ? data.response : null;
    return response && isRecord(response.error) ? { error: response.error } : { error: { message: 'Response failed' } };
  }
  if (event === 'error') return isRecord(data) ? data : { error: { message: String(data) } };
  if (isRecord(data) && isRecord(data.error) && !('choices' in data)) return data;
  return null;
}

export interface StreamMessage<T = unknown> {
  event: string;
  data: T;
}

export interface PostStreamOptions {
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Called with the response headers before the body is read (e.g. `x-splash-request-id`). */
  onHeaders?: (headers: Headers) => void;
}

/**
 * POSTs JSON and yields each event's parsed JSON payload until `[DONE]` or the end of
 * the stream. Throws ApiError for a non-2xx response or an in-stream error payload.
 * Abort via `options.signal` (Splash treats the disconnect as a cancel).
 */
export async function* postStream<T = unknown>(
  url: string,
  body: unknown,
  options: PostStreamOptions = {},
): AsyncGenerator<StreamMessage<T>> {
  const init: RequestInit = {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...options.headers },
    body: JSON.stringify(body),
  };
  if (options.signal) init.signal = options.signal;
  const res = await fetch(url, init);
  if (!res.ok) throw await errorFromResponse(res);
  options.onHeaders?.(res.headers);
  for await (const evt of readSse(res, options.signal)) {
    if (evt.data === '[DONE]') return;
    let data: unknown;
    try {
      data = JSON.parse(evt.data);
    } catch {
      data = evt.data;
    }
    const failure = streamError(evt.event, data);
    if (failure) {
      const parsed = parseErrorBody(failure, 500) ?? { message: String(evt.data), type: 'server_error', code: null };
      throw new ApiError(res.status, parsed, data);
    }
    yield { event: evt.event, data: data as T };
  }
}
