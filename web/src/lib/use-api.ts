import { useCallback, useEffect, useRef, useState } from 'preact/hooks';

export interface ApiState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** Refetch; keeps the old data visible while loading. */
  reload: () => Promise<void>;
  /** Replace the data locally (optimistic updates, SSE patches). */
  setData: (next: T | null | ((prev: T | null) => T | null)) => void;
}

/**
 * Small fetch hook for pages: runs `fetcher` on mount and whenever `deps` change, aborts the
 * stale request, and exposes reload/setData. Errors stay in `error` (render a LoadError band).
 */
export function useApi<T>(fetcher: (signal: AbortSignal) => Promise<T>, deps: readonly unknown[] = [], enabled = true): ApiState<T> {
  const [data, setDataState] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(enabled);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const ctrl = useRef<AbortController | null>(null);

  const run = useCallback(async () => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setLoading(true);
    try {
      const value = await fetchRef.current(c.signal);
      if (c.signal.aborted) return;
      setDataState(value);
      setError(null);
    } catch (err) {
      if (c.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return;
      setError(err);
    } finally {
      if (!c.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void run();
    return () => ctrl.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  const setData = useCallback((next: T | null | ((prev: T | null) => T | null)) => {
    setDataState((prev) => (typeof next === 'function' ? (next as (p: T | null) => T | null)(prev) : next));
  }, []);

  return { data, error, loading, reload: run, setData };
}
