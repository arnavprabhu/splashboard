import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import { ADMIN, api } from '../../api/client';
import type { LogBackfill, LogLine, LogTail } from '../../api/models';
import { subscribe, type Subscription } from '../../api/sse';
import { classify, EMPTY_BUFFER, metaRow, newFromBackfill, pushRows, type Buffer, type LogSource, type Row } from './model';

/** Initial tail before the stream opens (docs/ui/06 §4). */
export const TAIL_LINES = 1000;
const FLUSH_MS = 50;

/**
 * Buffers live at module level, one per source, so switching sources (or leaving the page and
 * coming back) shows the kept lines at once (L2) and the stream backfill only adds what is new.
 */
const buffers = new Map<LogSource, Buffer>();
let nextKey = 1;

export function resetLogBuffers(): void {
  buffers.clear();
}

export interface LogStream {
  buffer: Buffer;
  loading: boolean;
  error: unknown;
  /** Empties both sources' buffers (after Clear logs). */
  clear: () => void;
  retry: () => void;
}

/**
 * `GET /logs/{source}?tail=1000`, then `GET /logs/{source}/stream` (SSE `backfill` + `line`).
 * Lines arriving in bursts are flushed every 50 ms. A reconnect's backfill is merged; when it
 * does not line up with the buffer a "reconnected" row marks the possible gap.
 */
export function useLogStream(source: LogSource, command: string | null): LogStream {
  const [buffer, setBuffer] = useState<Buffer>(() => buffers.get(source) ?? EMPTY_BUFFER);
  const [loading, setLoading] = useState(() => !buffers.get(source)?.rows.length);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  const commandRef = useRef(command);
  commandRef.current = command;

  useEffect(() => {
    let cancelled = false;
    let sub: Subscription | null = null;
    let pending: Row[] = [];
    let timer: ReturnType<typeof setTimeout> | null = null;

    const update = (fn: (b: Buffer) => Buffer) => {
      const next = fn(buffers.get(source) ?? EMPTY_BUFFER);
      buffers.set(source, next);
      if (!cancelled) setBuffer(next);
    };
    const rowsOf = (lines: readonly LogLine[]) => lines.map((l) => classify(l, source, nextKey++, commandRef.current));
    const flush = () => {
      timer = null;
      const rows = pending;
      pending = [];
      update((b) => pushRows(b, rows));
    };

    setBuffer(buffers.get(source) ?? EMPTY_BUFFER);
    void (async () => {
      if (!buffers.get(source)?.rows.length) {
        setLoading(true);
        try {
          const tail = await api.get<LogTail>(`/logs/${source}`, { tail: TAIL_LINES });
          if (cancelled) return;
          update((b) => pushRows(b, rowsOf(tail.lines ?? [])));
          setError(null);
        } catch (err) {
          if (cancelled) return;
          setError(err);
        }
      }
      if (cancelled) return;
      setLoading(false);
      sub = subscribe<unknown>(`${ADMIN}/logs/${source}/stream`, {
        events: ['backfill', 'line'],
        onMessage: ({ event, data }) => {
          if (event === 'backfill') {
            const lines = (data as LogBackfill | null)?.lines ?? [];
            if (timer !== null) {
              clearTimeout(timer);
              flush();
            }
            update((b) => {
              const merged = newFromBackfill(b.rows, rowsOf(lines));
              return pushRows(b, merged.gap ? [metaRow('reconnected', nextKey++), ...merged.rows] : merged.rows);
            });
            setError(null);
          } else if (event === 'line' && data && typeof data === 'object') {
            pending.push(...rowsOf([data as LogLine]));
            timer ??= setTimeout(flush, FLUSH_MS);
          }
        },
      });
    })();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
      if (pending.length) {
        const rows = pending;
        pending = [];
        buffers.set(source, pushRows(buffers.get(source) ?? EMPTY_BUFFER, rows));
      }
      sub?.close();
    };
  }, [source, attempt]);

  const clear = useCallback(() => {
    buffers.set('engine', EMPTY_BUFFER);
    buffers.set('manager', EMPTY_BUFFER);
    setBuffer(EMPTY_BUFFER);
  }, []);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  return { buffer, loading, error, clear, retry };
}
