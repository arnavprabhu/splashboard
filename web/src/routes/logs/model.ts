/**
 * Pure logic for the Logs live tail: engine level heuristics on top of the
 * manager's `LogLine.level`, session dividers, structured-event tags, the 5,000-line ring
 * buffer, level / Requests / search filters and backfill merging.
 *
 * Session dividers are pattern-matched from text until `LogLine.kind` exists: the supervisor writes `=== Splashboard: engine session started · <model> ·
 * <command>` and `=== Splashboard: engine session ended · <reason> · exit <code>`
 * (manager/splash_gui/logging_setup.py SESSION_START / SESSION_END). Logs written before the
 * rename say `Splash GUI:` and still match.
 */

import type { LogLine } from '../../api/models';

export type LogSource = 'engine' | 'manager';
export type DisplayLevel = 'debug' | 'info' | 'req' | 'warn' | 'error';
export type LevelFilter = 'all' | 'info' | 'warn' | 'error';
export type EventTag = 'load' | 'ready' | 'template' | 'budget' | 'ssd' | 'trace';
export type MetaKind = 'dropped' | 'reconnected';

export const BUFFER_LINES = 5000;
export const TRUNCATE_AT = 4000;
export const LEVEL_FILTERS: readonly LevelFilter[] = ['all', 'info', 'warn', 'error'];

export interface Divider {
  kind: 'start' | 'stop';
  model: string | null;
  command: string | null;
  reason: string | null;
  exit: string | null;
}

export interface Row {
  /** Unique within one buffer; used as the render key. */
  key: number;
  ts: string | null;
  /** `HH:MM:SS` shown in the time column. */
  clock: string | null;
  level: DisplayLevel;
  stream: 'stdout' | 'stderr' | null;
  /** ANSI-stripped text without the engine's own `HH:MM:SS ` prefix. */
  text: string;
  tag: EventTag | null;
  divider: Divider | null;
  meta: MetaKind | null;
}

// ---------- classification ----------

// eslint-disable-next-line no-control-regex
const ANSI = /\u001b\[[0-9;?]*[ -/]*[@-~]|\u001b[@-_]/g;
const CLOCK = /^(\d\d:\d\d:\d\d) /;
const START = /^=+ (?:Splashboard|Splash GUI): engine session started(?: · (.*))?$/;
const STOP = /^=+ (?:Splashboard|Splash GUI): engine session ended(?: · (.*))?$/;
/** First match wins, applied to engine lines only. */
const ENGINE_ERROR = /engine_failed|Traceback|fatal|metal_failure|crash trace/i;
const ENGINE_REQ = /^(Done|Cancelled) · /;
const ENGINE_WARN = /recovering|may not hold|Hub unreachable|cannot be installed|exceeds the|warning|refused|disabled/i;

export function stripAnsi(text: string): string {
  return text.replace(ANSI, '');
}

/** Parses a manager-written session line, or a line that is the effective command itself. */
export function parseDivider(text: string, command?: string | null): Divider | null {
  const start = START.exec(text);
  if (start) {
    const rest = start[1] ?? '';
    const sep = rest.indexOf(' · ');
    const model = sep >= 0 ? rest.slice(0, sep) : rest || null;
    const cmd = sep >= 0 ? rest.slice(sep + 3) : null;
    return { kind: 'start', model: model || null, command: cmd || null, reason: null, exit: null };
  }
  const stop = STOP.exec(text);
  if (stop) {
    const parts = (stop[1] ?? '').split(' · ').filter(Boolean);
    const exitPart = parts.find((p) => p.startsWith('exit '));
    return {
      kind: 'stop',
      model: null,
      command: null,
      reason: parts[0] && !parts[0].startsWith('exit ') ? parts[0] : null,
      exit: exitPart ? exitPart.slice(5) : null,
    };
  }
  if (command && text.trim() === command.trim()) {
    return { kind: 'start', model: null, command, reason: null, exit: null };
  }
  return null;
}

export function tagFor(text: string): EventTag | null {
  if (/^Weights (loaded|restored) in /.test(text)) return 'load';
  if (/^Ready · /.test(text)) return 'ready';
  if (/^Chat template · /.test(text)) return 'template';
  if (/^(physical memory|KV capacity of one request):/i.test(text)) return 'budget';
  if (/crash trace/i.test(text)) return 'trace';
  if (/--max-cache-disk/.test(text)) return 'ssd';
  return null;
}

/** Local `HH:MM:SS` of an ISO timestamp. */
export function clockOf(ts: string | null | undefined): string | null {
  if (!ts) return null;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return null;
  const pad = (v: number) => String(v).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Local `YYYY-MM-DD HH:MM:SS` for the time column's tooltip. */
export function fullTimeOf(ts: string | null | undefined): string | null {
  if (!ts) return null;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return null;
  const pad = (v: number) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clockOf(ts)}`;
}

export function engineLevel(base: LogLine['level'], stream: LogLine['stream'], body: string): DisplayLevel {
  if (stream === 'stderr' && body.startsWith('Error ·')) return 'error';
  if (ENGINE_ERROR.test(body)) return 'error';
  if (base === 'error') return 'error';
  if (ENGINE_REQ.test(body)) return 'req';
  if (base === 'warn' || ENGINE_WARN.test(body)) return 'warn';
  return base === 'debug' ? 'debug' : 'info';
}

export function classify(line: LogLine, source: LogSource, key: number, command?: string | null): Row {
  const clean = stripAnsi(line.text ?? '');
  const divider = parseDivider(clean, command);
  let clock: string | null = null;
  let body = clean;
  if (source === 'engine') {
    const m = CLOCK.exec(clean);
    if (m) {
      clock = m[1] ?? null;
      body = clean.slice(m[0].length);
    }
  }
  const level: DisplayLevel = divider ? 'info' : source === 'engine' ? engineLevel(line.level, line.stream ?? null, body) : line.level;
  return {
    key,
    ts: line.ts ?? null,
    clock: clock ?? clockOf(line.ts),
    level,
    stream: line.stream ?? null,
    text: body,
    tag: divider ? null : tagFor(body),
    divider,
    meta: null,
  };
}

export function metaRow(kind: MetaKind, key: number): Row {
  return { key, ts: null, clock: null, level: 'info', stream: null, text: '', tag: null, divider: null, meta: kind };
}

// ---------- ring buffer ----------

export interface Buffer {
  rows: Row[];
  /** Lines that fell off the top. */
  dropped: number;
  /** Lines ever appended (new-lines counter while not following). */
  appended: number;
}

export const EMPTY_BUFFER: Buffer = { rows: [], dropped: 0, appended: 0 };

/** Appends rows and keeps the newest `cap`; a `dropped` meta row marks the cut. */
export function pushRows(buf: Buffer, rows: readonly Row[], cap = BUFFER_LINES): Buffer {
  if (rows.length === 0) return buf;
  const lines = rows.filter((r) => r.meta === null).length;
  let next = [...buf.rows.filter((r) => r.meta !== 'dropped'), ...rows];
  let dropped = buf.dropped;
  const over = next.length - cap;
  if (over > 0) {
    dropped += next.slice(0, over).filter((r) => r.meta === null).length;
    next = next.slice(over);
  }
  if (dropped > 0) next = [metaRow('dropped', -1), ...next];
  return { rows: next, dropped, appended: buf.appended + lines };
}

// ---------- backfill merge ----------

function sameLine(a: Row, b: Row): boolean {
  return a.meta === null && b.meta === null && a.ts === b.ts && a.text === b.text && a.stream === b.stream;
}

/**
 * Rows of a stream backfill (the last ≤ 200 lines when the stream opened) that are not
 * already in the buffer. The buffer's last line is looked up in the backfill; when it is
 * missing the offsets don't line up (reconnect after a gap), so everything is new and the
 * caller inserts a "reconnected" meta row.
 */
export function newFromBackfill(existing: readonly Row[], backfill: readonly Row[]): { rows: Row[]; gap: boolean } {
  const last = [...existing].reverse().find((r) => r.meta === null);
  if (!last) return { rows: [...backfill], gap: false };
  for (let i = backfill.length - 1; i >= 0; i--) {
    if (sameLine(backfill[i]!, last)) return { rows: backfill.slice(i + 1), gap: false };
  }
  return { rows: [...backfill], gap: backfill.length > 0 };
}

// ---------- filters ----------

export function parseLevel(value: string | null | undefined): LevelFilter {
  return (LEVEL_FILTERS as readonly string[]).includes(value ?? '') ? (value as LevelFilter) : 'all';
}

/** Threshold semantics: Info hides debug, Warn shows warn+error, Error shows errors only. */
export function levelPasses(level: DisplayLevel, filter: LevelFilter): boolean {
  if (filter === 'all') return true;
  if (filter === 'error') return level === 'error';
  if (filter === 'warn') return level === 'warn' || level === 'error';
  return level !== 'debug';
}

export function rowMatches(row: Row, query: string): boolean {
  if (!query || row.meta) return false;
  const q = query.toLowerCase();
  if (row.divider) return (row.divider.command ?? row.text).toLowerCase().includes(q) || (row.divider.model ?? '').toLowerCase().includes(q);
  return row.text.toLowerCase().includes(q);
}

export interface FilterOptions {
  level: LevelFilter;
  requests: boolean;
  query: string;
  /** Show only matching lines (FILTER mode) instead of highlighting in place. */
  filterMode: boolean;
}

/** Dividers and meta rows never filter out. */
export function visibleRows(rows: readonly Row[], { level, requests, query, filterMode }: FilterOptions): Row[] {
  return rows.filter((r) => {
    if (r.divider || r.meta) return true;
    if (!requests && r.level === 'req') return false;
    if (!levelPasses(r.level, level)) return false;
    if (filterMode && query && !rowMatches(r, query)) return false;
    return true;
  });
}

/** Indices (into `rows`) of the rows that match the search. */
export function matchIndices(rows: readonly Row[], query: string): number[] {
  if (!query) return [];
  const out: number[] = [];
  rows.forEach((r, i) => {
    if (rowMatches(r, query)) out.push(i);
  });
  return out;
}

export function levelCounts(rows: readonly Row[]): Record<'warn' | 'error', number> {
  let warn = 0;
  let error = 0;
  for (const r of rows) {
    if (r.divider || r.meta) continue;
    if (r.level === 'warn') warn += 1;
    else if (r.level === 'error') error += 1;
  }
  return { warn, error };
}

/** Splits text around case-insensitive matches for highlighting. */
export function splitMatches(text: string, query: string): Array<{ text: string; match: boolean }> {
  if (!query) return [{ text, match: false }];
  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  const out: Array<{ text: string; match: boolean }> = [];
  let i = 0;
  let j = lower.indexOf(q);
  while (j >= 0) {
    if (j > i) out.push({ text: text.slice(i, j), match: false });
    out.push({ text: text.slice(j, j + q.length), match: true });
    i = j + q.length;
    j = lower.indexOf(q, i);
  }
  if (i < text.length) out.push({ text: text.slice(i), match: false });
  return out;
}
