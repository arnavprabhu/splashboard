/**
 * GET SSE subscriptions (EventSource) with reconnect and backoff, for the manager's
 * streaming routes: /api/admin/events, /metrics/live, /logs/{source}/stream.
 */

export interface SseMessage<T = unknown> {
  event: string;
  data: T;
  id: string;
}

export interface SubscribeOptions<T> {
  /** Named events to listen for besides the default "message". */
  events?: readonly string[];
  onMessage: (message: SseMessage<T>) => void;
  onOpen?: () => void;
  onError?: (attempt: number) => void;
  /** Parse data as JSON (default true). Unparseable data is passed through as a string. */
  json?: boolean;
  minDelayMs?: number;
  maxDelayMs?: number;
  /** Injectable for tests. */
  EventSourceImpl?: typeof EventSource;
}

export interface Subscription {
  close(): void;
  readonly connected: boolean;
}

export function backoffDelay(attempt: number, min = 500, max = 15_000): number {
  return Math.min(max, min * 2 ** Math.max(0, attempt - 1));
}

export function subscribe<T = unknown>(url: string, options: SubscribeOptions<T>): Subscription {
  const Impl = options.EventSourceImpl ?? globalThis.EventSource;
  const { minDelayMs = 500, maxDelayMs = 15_000, json = true } = options;
  let source: EventSource | null = null;
  let closed = false;
  let connected = false;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const deliver = (event: string) => (e: MessageEvent<string>) => {
    let data: unknown = e.data;
    if (json) {
      try {
        data = JSON.parse(e.data);
      } catch {
        /* keep the string */
      }
    }
    options.onMessage({ event, data: data as T, id: e.lastEventId ?? '' });
  };

  const connect = () => {
    if (closed) return;
    const es = new Impl(url, { withCredentials: true });
    source = es;
    es.onopen = () => {
      attempt = 0;
      connected = true;
      options.onOpen?.();
    };
    es.onmessage = deliver('message');
    for (const name of options.events ?? []) es.addEventListener(name, deliver(name) as EventListener);
    es.onerror = () => {
      connected = false;
      attempt += 1;
      options.onError?.(attempt);
      // The browser retries on its own while CONNECTING; take over once it gives up.
      if (es.readyState === Impl.CLOSED || attempt > 1) {
        es.close();
        if (source === es) source = null;
        if (!closed && timer === null) {
          timer = setTimeout(() => {
            timer = null;
            connect();
          }, backoffDelay(attempt, minDelayMs, maxDelayMs));
        }
      }
    };
  };

  connect();

  return {
    close() {
      closed = true;
      connected = false;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      source?.close();
      source = null;
    },
    get connected() {
      return connected;
    },
  };
}
