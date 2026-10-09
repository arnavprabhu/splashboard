/**
 * Usage history: filters in the query string, the request
 * parameters each band sends, and the pure transforms behind the bands.
 */
import type { UsagePoint, UsageSummary } from '../../api/models';

export const HISTORY_PAGE_SIZE = 50;
/** Matches the manager's `/usage/timeseries` default window. */
export const DEFAULT_RANGE_DAYS = 30;

/** The proxied endpoints usage is recorded for. */
export const USAGE_ENDPOINTS = [
  '/v1/chat/completions',
  '/v1/completions',
  '/v1/responses',
  '/v1/messages',
  '/v1/messages/count_tokens',
  '/v1/judgments',
  '/v1/systemone',
  '/tokenize',
  '/apply-template',
] as const;

/** Status filter options; the manager's facets list the same set. */
export const USAGE_STATUS = ['2xx', '4xx', '5xx', 'cancelled'] as const;

export interface HistoryFilters {
  /** Local dates, YYYY-MM-DD. */
  from: string;
  to: string;
  model: string;
  endpoint: string;
  status: string;
  client: string;
}

export const FILTER_KEYS = ['from', 'to', 'model', 'endpoint', 'status', 'client'] as const;

const pad = (n: number) => String(n).padStart(2, '0');

export function localDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function defaultRange(now: Date = new Date()): { from: string; to: string } {
  const from = new Date(now);
  from.setDate(from.getDate() - (DEFAULT_RANGE_DAYS - 1));
  return { from: localDate(from), to: localDate(now) };
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Reads the filters from the page's query string; missing or malformed dates use the default range. */
export function readFilters(params: URLSearchParams, now: Date = new Date()): HistoryFilters {
  const range = defaultRange(now);
  const date = (key: 'from' | 'to') => {
    const v = params.get(key) ?? '';
    return DATE_RE.test(v) ? v : range[key];
  };
  let from = date('from');
  let to = date('to');
  if (from > to) [from, to] = [to, from];
  return {
    from,
    to,
    model: params.get('model') ?? '',
    endpoint: params.get('endpoint') ?? '',
    status: params.get('status') ?? '',
    client: params.get('client') ?? '',
  };
}

/** Start of the local day as an ISO timestamp with its offset. */
export function dayStartIso(date: string): string {
  const [y, m, d] = date.split('-').map(Number);
  return new Date(y!, m! - 1, d!, 0, 0, 0, 0).toISOString();
}

/** End of the local day (23:59:59.999). */
export function dayEndIso(date: string): string {
  const [y, m, d] = date.split('-').map(Number);
  return new Date(y!, m! - 1, d!, 23, 59, 59, 999).toISOString();
}

type Query = Record<string, string | undefined>;

/** The manager's filter names (`start`/`end`) for one set of page filters. */
export function apiFilters(f: HistoryFilters): Query {
  return {
    start: dayStartIso(f.from),
    end: dayEndIso(f.to),
    model: f.model || undefined,
    endpoint: f.endpoint || undefined,
    status: f.status || undefined,
    client: f.client || undefined,
  };
}

/** `/api/admin/usage/export.csv?…` carrying exactly the active filters. */
export function exportHref(f: HistoryFilters): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(apiFilters(f))) if (v) q.set(k, v);
  return `/api/admin/usage/export.csv?${q.toString()}`;
}

/** True when any filter differs from the defaults (enables Reset). */
export function isFiltered(f: HistoryFilters, now: Date = new Date()): boolean {
  const range = defaultRange(now);
  return f.from !== range.from || f.to !== range.to || !!(f.model || f.endpoint || f.status || f.client);
}

// ---------- Tokens per day (stacked bars per model) ----------

export interface StackedSeries {
  label: string;
  tone: 'acc' | 'ink' | 'mute';
  /** Legend glyph: solid, outlined, or the "other" mark. */
  glyph: string;
}

export interface Stacked {
  /** x (unix seconds) then one cumulative column per series, largest (top of the stack) first. */
  data: number[][];
  series: StackedSeries[];
}

/**
 * Up to two named models (the active one first, in the accent) plus "other". Bars are drawn
 * as cumulative totals, tallest first, so later (shorter) bars sit on top: a stack without
 * uPlot's band plugin.
 */
export function stackTokensPerDay(points: readonly UsagePoint[], active: string | null, otherLabel: string): Stacked {
  const totals = new Map<string, number>();
  for (const p of points) {
    const g = p.group ?? otherLabel;
    totals.set(g, (totals.get(g) ?? 0) + p.prompt_tokens + p.completion_tokens);
  }
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([g]) => g);
  const named: string[] = [];
  if (active && totals.has(active)) named.push(active);
  for (const g of ranked) if (named.length < 2 && g !== otherLabel && !named.includes(g)) named.push(g);
  const hasOther = ranked.some((g) => !named.includes(g));
  const groups = hasOther ? [...named, otherLabel] : named;
  const xs = [...new Set(points.map((p) => p.t))].sort();
  const byX = new Map(xs.map((t, i) => [t, i]));
  const cols = groups.map(() => xs.map(() => 0));
  for (const p of points) {
    const i = byX.get(p.t)!;
    const g = p.group ?? otherLabel;
    const gi = named.includes(g) ? named.indexOf(g) : groups.length - 1;
    cols[gi]![i]! += p.prompt_tokens + p.completion_tokens;
  }
  // cumulative from the bottom of the stack (index 0) upward
  const cum = cols.map((c) => [...c]);
  for (let gi = 1; gi < cum.length; gi++) for (let i = 0; i < xs.length; i++) cum[gi]![i]! += cum[gi - 1]![i]!;
  const tones: Array<'acc' | 'ink' | 'mute'> = groups.map((g, gi) => {
    if (hasOther && gi === groups.length - 1) return 'mute';
    if (active && g === active) return 'acc';
    return 'ink';
  });
  const series = groups.map((g, gi) => ({
    label: g,
    tone: tones[gi]!,
    glyph: tones[gi] === 'mute' ? '▨' : '■',
  }));
  return {
    data: [xs.map((t) => Date.parse(t) / 1000), ...cum.reverse()],
    series: series.reverse(),
  };
}

// ---------- Requests over time ----------

/** Requests and failures per bucket, summed across groups. */
export function requestsOverTime(points: readonly UsagePoint[]): number[][] {
  const by = new Map<string, { requests: number; errors: number }>();
  for (const p of points) {
    const cur = by.get(p.t) ?? { requests: 0, errors: 0 };
    cur.requests += p.requests;
    cur.errors += p.errors;
    by.set(p.t, cur);
  }
  const xs = [...by.keys()].sort();
  return [xs.map((t) => Date.parse(t) / 1000), xs.map((t) => by.get(t)!.requests), xs.map((t) => by.get(t)!.errors)];
}

// ---------- Heatmap ----------

/** Ink opacity for a cell: 0 for no requests, otherwise 0.08–1.0 by share of the busiest hour. */
export function heatOpacity(n: number, max: number): number {
  if (!(n > 0) || !(max > 0)) return 0;
  return Math.round((0.08 + 0.92 * Math.min(1, n / max)) * 1000) / 1000;
}

export function heatMax(grid: readonly (readonly number[])[]): number {
  return grid.reduce((m, row) => Math.max(m, ...row), 0);
}

// ---------- Top clients ----------

export interface ClientRow {
  client: string;
  requests: number;
  tokens: number | null;
  last_seen: string | null;
}

export function clientRows(summary: UsageSummary | null): ClientRow[] {
  return (summary?.top_clients ?? []).map((c) => ({
    client: c.client,
    requests: c.requests,
    tokens: typeof c.total_tokens === 'number' ? c.total_tokens : null,
    last_seen: c.last_seen_at ?? null,
  }));
}

/** "Rows 1–50 of 1,284" for a 0-based page. */
export function pageRange(page: number, shown: number, size = HISTORY_PAGE_SIZE): { a: number; b: number } {
  return { a: shown > 0 ? page * size + 1 : 0, b: page * size + shown };
}

/** Number of injected fields for the INJECTED column. */
export function injectedCount(injected: Record<string, unknown> | null | undefined): number {
  return injected ? Object.keys(injected).length : 0;
}
