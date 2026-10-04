/** Display formatters. Every function returns DASH for missing or non-finite input. */

export const DASH = '—';

type Num = number | null | undefined;

const isNum = (v: Num): v is number => typeof v === 'number' && Number.isFinite(v);

function trimFixed(value: number, digits: number): string {
  return value.toFixed(digits).replace(/\.0+$|(\.\d*?)0+$/, '$1');
}

export interface BytesOptions {
  /** 1024 (default) matches Splash's K/M/G suffixes; 1000 matches Hub download sizes. */
  base?: 1024 | 1000;
  digits?: number;
}

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'] as const;

export function formatBytes(bytes: Num, { base = 1024, digits = 1 }: BytesOptions = {}): string {
  if (!isNum(bytes)) return DASH;
  const sign = bytes < 0 ? '-' : '';
  let value = Math.abs(bytes);
  let unit = 0;
  while (value >= base && unit < BYTE_UNITS.length - 1) {
    value /= base;
    unit += 1;
  }
  const text = unit === 0 ? String(Math.round(value)) : trimFixed(value, value >= 100 ? 0 : digits);
  return `${sign}${text} ${BYTE_UNITS[unit]}`;
}

export function formatBytesPerSecond(bps: Num, options?: BytesOptions): string {
  if (!isNum(bps)) return DASH;
  return `${formatBytes(bps, options)}/s`;
}

/** Tokens per second: 1 decimal below 100, whole numbers above. */
export function formatTokPerSec(value: Num, { unit = true }: { unit?: boolean } = {}): string {
  if (!isNum(value)) return DASH;
  const text = value >= 100 ? Math.round(value).toLocaleString('en-US') : value.toFixed(1);
  return unit ? `${text} tok/s` : text;
}

export function formatCount(value: Num): string {
  if (!isNum(value)) return DASH;
  return Math.round(value).toLocaleString('en-US');
}

/** 1234 -> "1.2K", 3_400_000 -> "3.4M". */
export function formatCompact(value: Num): string {
  if (!isNum(value)) return DASH;
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  const steps: Array<[number, string]> = [
    [1e12, 'T'],
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'K'],
  ];
  for (const [size, suffix] of steps) {
    if (abs >= size) {
      const scaled = abs / size;
      return `${sign}${trimFixed(scaled, scaled >= 100 ? 0 : 1)}${suffix}`;
    }
  }
  return `${sign}${Math.round(abs)}`;
}

/** A ratio in [0, 1] as a percentage. */
export function formatPercent(ratio: Num, digits = 1): string {
  if (!isNum(ratio)) return DASH;
  const pct = ratio * 100;
  if (pct !== 0 && Math.abs(pct) < 10 ** -digits) return pct > 0 ? `<${10 ** -digits}%` : DASH;
  return `${trimFixed(pct, digits)}%`;
}

/** Milliseconds, for latencies: "850 ms", "1.24 s", "2 m 05 s". */
export function formatMs(ms: Num): string {
  if (!isNum(ms)) return DASH;
  if (ms < 0) return DASH;
  if (ms < 10) return `${trimFixed(ms, 1)} ms`;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${trimFixed(ms / 1000, ms < 10_000 ? 2 : 1)} s`;
  return formatDuration(ms / 1000);
}

/** Seconds, for uptimes and ETAs: "45 s", "3 m 20 s", "2 h 05 m", "3 d 4 h". */
export function formatDuration(seconds: Num): string {
  if (!isNum(seconds) || seconds < 0) return DASH;
  const s = Math.floor(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m ${String(s % 60).padStart(2, '0')} s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${String(m % 60).padStart(2, '0')} m`;
  const d = Math.floor(h / 24);
  return `${d} d ${h % 24} h`;
}

/** "just now", "3 min ago", "in 2 h", "yesterday", "5 d ago", then an absolute YYYY-MM-DD date. */
export function formatRelativeTime(
  when: Date | number | string | null | undefined,
  now: Date | number = Date.now(),
): string {
  if (when === null || when === undefined) return DASH;
  const t = when instanceof Date ? when.getTime() : typeof when === 'string' ? Date.parse(when) : when;
  const n = now instanceof Date ? now.getTime() : now;
  if (!Number.isFinite(t)) return DASH;
  const diff = (n - t) / 1000;
  const future = diff < 0;
  const abs = Math.abs(diff);
  const phrase = (v: number, unit: string) => (future ? `in ${v} ${unit}` : `${v} ${unit} ago`);
  if (abs < 45) return 'just now';
  const minutes = Math.round(abs / 60);
  if (minutes < 60) return phrase(minutes, 'min');
  const hours = Math.round(abs / 3600);
  if (hours < 24) return phrase(hours, 'h');
  const days = Math.round(abs / 86400);
  if (days === 1) return future ? 'tomorrow' : 'yesterday';
  if (days < 7) return phrase(days, 'd');
  return formatDate(t);
}

/** Absolute local date, YYYY-MM-DD. */
export function formatDate(when: Date | number): string {
  const d = when instanceof Date ? when : new Date(when);
  if (Number.isNaN(d.getTime())) return DASH;
  const pad = (v: number) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Context sizes: 131072 -> "128K", 1000 -> "1,000". */
export function formatTokens(tokens: Num): string {
  if (!isNum(tokens)) return DASH;
  if (tokens >= 1024 && tokens % 1024 === 0) return `${tokens / 1024}K`;
  return formatCount(tokens);
}

/** Two-digit list index used by list rows: 1 -> "01". */
export function formatIndex(i: number): string {
  return String(i).padStart(2, '0');
}
