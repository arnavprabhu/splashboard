/**
 * Client-side ring buffer for the live charts (docs/ui/02 §7): backfilled from
 * GET /metrics/series (docs/api.md §5.2), then appended with each /metrics/live sample.
 * Gaps (reconnects, engine restarts) are a null row, which uPlot draws as a break.
 */

import type { LiveMetrics, MetricsSeries } from '../../api/models';

/** Every series key the charts draw (dotted LiveMetrics paths, as MetricsSeries names them). */
export const SERIES_KEYS = [
  'throughput.decode_tps',
  'throughput.prefill_tps',
  'scheduler.decoding',
  'scheduler.prefilling',
  'scheduler.queued',
  'scheduler.waiting_resources',
  'latency.ttft_p50_ms',
  'latency.ttft_p95_ms',
  'memory.charged_bytes',
  'memory.current_bytes',
  'memory.peak_bytes',
  'memory.limit_bytes',
  'kv.pages_active',
  'kv.pages_cache',
  'kv.pages_free',
  'cache.hit_rate',
  'cache.efficiency',
  'disk.read_bps',
  'disk.written_bps',
] as const;

export type SeriesKey = (typeof SERIES_KEYS)[number];

export const MAX_WINDOW_S = 3600;
/** A jump longer than this between samples is a gap. */
export const GAP_S = 5;

export interface SeriesBuffer {
  t: number[];
  cols: Record<SeriesKey, Array<number | null>>;
}

export function emptyBuffer(): SeriesBuffer {
  const cols = {} as Record<SeriesKey, Array<number | null>>;
  for (const k of SERIES_KEYS) cols[k] = [];
  return { t: [], cols };
}

function pick(sample: LiveMetrics, key: SeriesKey): number | null {
  const [group, field] = key.split('.') as [string, string];
  const g = (sample as unknown as Record<string, unknown>)[group];
  if (!g || typeof g !== 'object') return null;
  const v = (g as Record<string, unknown>)[field];
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function pushRow(buf: SeriesBuffer, t: number, value: (k: SeriesKey) => number | null): void {
  buf.t.push(t);
  for (const k of SERIES_KEYS) buf.cols[k].push(value(k));
}

/** Drops samples older than `MAX_WINDOW_S` before `now` (mutates). */
export function trim(buf: SeriesBuffer, now: number): void {
  const cutoff = now - MAX_WINDOW_S;
  let i = 0;
  while (i < buf.t.length && (buf.t[i] ?? 0) < cutoff) i += 1;
  if (i === 0) return;
  buf.t.splice(0, i);
  for (const k of SERIES_KEYS) buf.cols[k].splice(0, i);
}

/** Builds a buffer from GET /metrics/series (unknown keys ignored, missing keys as null). */
export function fromSeries(series: MetricsSeries | null | undefined): SeriesBuffer {
  const buf = emptyBuffer();
  if (!series || !Array.isArray(series.t)) return buf;
  series.t.forEach((t, i) => {
    pushRow(buf, t, (k) => {
      const v = series.series?.[k]?.[i];
      return typeof v === 'number' && Number.isFinite(v) ? v : null;
    });
  });
  return buf;
}

/**
 * Appends one live sample (mutates). Older-or-equal timestamps are ignored; a long gap or an
 * engine restart first inserts a null row so the line breaks there.
 */
export function append(buf: SeriesBuffer, sample: LiveMetrics): boolean {
  const last = buf.t[buf.t.length - 1];
  if (last !== undefined && sample.t <= last) return false;
  if (last !== undefined && (sample.t - last > GAP_S || sample.restarted)) {
    pushRow(buf, Math.min(last + 1, sample.t - 0.001), () => null);
  }
  pushRow(buf, sample.t, (k) => pick(sample, k));
  trim(buf, sample.t);
  return true;
}

/** Merges a backfill into a live buffer: backfill rows older than the first live row, then live. */
export function merge(backfill: SeriesBuffer, live: SeriesBuffer): SeriesBuffer {
  const first = live.t[0];
  const out = emptyBuffer();
  backfill.t.forEach((t, i) => {
    if (first === undefined || t < first) pushRow(out, t, (k) => backfill.cols[k][i] ?? null);
  });
  live.t.forEach((t, i) => pushRow(out, t, (k) => live.cols[k][i] ?? null));
  return out;
}

/**
 * Columns for one chart over the last `windowS` seconds. A leading null row at `now - windowS`
 * pins the x axis to the full window even before it has filled up.
 */
export function windowData(buf: SeriesBuffer, keys: readonly SeriesKey[], windowS: number, now: number): [number[], ...Array<Array<number | null>>] {
  const start = now - windowS;
  let i = 0;
  while (i < buf.t.length && (buf.t[i] ?? 0) < start) i += 1;
  const xs = buf.t.slice(i);
  const cols = keys.map((k) => buf.cols[k].slice(i));
  if (xs.length === 0 || (xs[0] ?? 0) > start + 1) {
    xs.unshift(start);
    for (const c of cols) c.unshift(null);
  }
  return [xs, ...cols];
}

/** Latest non-null value of a column and its peak inside the window. */
export function lastAndPeak(col: ReadonlyArray<number | null>): { last: number | null; peak: number | null } {
  let last: number | null = null;
  let peak: number | null = null;
  for (const v of col) {
    if (v === null) continue;
    last = v;
    peak = peak === null ? v : Math.max(peak, v);
  }
  return { last, peak };
}

export const WINDOWS = [5, 15, 60] as const;
export type WindowMin = (typeof WINDOWS)[number];

export function readWindow(v: string | null | undefined): WindowMin {
  const n = Number(v);
  return (WINDOWS as readonly number[]).includes(n) ? (n as WindowMin) : 15;
}

export function nextWindow(w: WindowMin): WindowMin {
  const i = WINDOWS.indexOf(w);
  return WINDOWS[(i + 1) % WINDOWS.length] ?? 15;
}
