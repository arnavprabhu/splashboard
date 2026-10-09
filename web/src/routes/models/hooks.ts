import { useCallback, useEffect, useRef } from 'preact/hooks';
import { useSearchParams } from 'wouter-preact';
import type { DownloadItem } from '../../api/models';
import { useTitle } from '../../lib/title';
import { activeDownloads, downloadProgress, downloads, refreshDownloads, useEvent } from '../../store';
import { useApi } from '../../lib/use-api';
import { listModels } from './api';
import { pageTitle } from './logic';

/** `42% · Models — Splashboard` while a download runs, updated at most once per second. */
export function useModelsTitle(page: string): void {
  const progress = downloadProgress.value;
  const last = useRef(0);
  const shown = useRef<string>(pageTitle(page, progress));
  const now = Date.now();
  const next = pageTitle(page, progress);
  if (next !== shown.current && (now - last.current >= 1000 || progress === null)) {
    shown.current = next;
    last.current = now;
  }
  useTitle(shown.current);
}

/** Polls GET /downloads every 2 s while anything is queued or running (events may be down). */
export function useDownloadsPoll(): void {
  const active = activeDownloads.value.length > 0;
  useEffect(() => {
    void refreshDownloads();
  }, []);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => void refreshDownloads(), 2000);
    return () => clearInterval(timer);
  }, [active]);
}

/** Calls `fn` once per download that reaches `state` while the page is open. */
export function useDownloadTransition(state: DownloadItem['state'], fn: (item: DownloadItem) => void): void {
  const seen = useRef<Map<string, string> | null>(null);
  const ref = useRef(fn);
  ref.current = fn;
  const list = downloads.value;
  useEffect(() => {
    const prev = seen.current;
    const next = new Map(list.map((d) => [d.id, d.state] as const));
    seen.current = next;
    if (!prev) return;
    for (const d of list) {
      const before = prev.get(d.id);
      if (d.state === state && before !== undefined && before !== state) ref.current(d);
    }
  }, [list, state]);
}

/** The installed list, refetched on `models.changed` and when a download finishes. */
export function useInstalled() {
  const state = useApi((signal) => listModels(signal), []);
  useEvent('models.changed', () => void state.reload());
  useDownloadTransition('done', () => void state.reload());
  return state;
}

/** `?model=<id>` opens the detail drawer on either tab; closing removes it. */
export function useDrawerParam(): [string | null, (id: string) => void, () => void] {
  const [params, setParams] = useSearchParams();
  const id = params.get('model');
  const open = useCallback(
    (model: string) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set('model', model);
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
  const close = useCallback(
    () =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete('model');
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
  return [id, open, close];
}
