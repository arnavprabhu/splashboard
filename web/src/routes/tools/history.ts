/**
 * Playground history (SPEC §10.6, docs/ui/08 §1.8): the last 50 requests in localStorage key
 * `playground.history`. Request bodies are kept (data URLs truncated to 64 characters), response
 * bodies are not: only the first 120 characters of the parsed text or error (D-08-2).
 */

export const HISTORY_KEY = 'playground.history';
export const HISTORY_MAX = 50;
export const SUMMARY_MAX = 120;
export const DATA_URL_KEEP = 64;
export const TRUNCATED = '…[truncated]';

export type Mode = 'profiles' | 'raw';

export interface HistoryEntry {
  id: string;
  /** ms since the epoch. */
  ts: number;
  method: string;
  path: string;
  mode: Mode;
  model: string | null;
  status: number | null;
  duration_ms: number | null;
  /** The request body text as sent (data URLs truncated), or null for GET/DELETE. */
  body: string | null;
  response_summary: string;
  /** True when a data URL in the body was cut. */
  truncated?: boolean;
}

const DATA_URL = /data:[a-z0-9.+/-]+;base64,[A-Za-z0-9+/=]+/gi;
/** Base64 attachment payloads in Anthropic blocks: {"type":"base64",…,"data":"…"}. */
const B64_FIELD = /("data"\s*:\s*")([A-Za-z0-9+/=]{65,})(")/g;

/** Cuts data URLs and base64 `data` fields to 64 characters plus a marker. */
export function truncateDataUrls(body: string): { text: string; truncated: boolean } {
  let truncated = false;
  let text = body.replace(DATA_URL, (m) => {
    if (m.length <= DATA_URL_KEEP) return m;
    truncated = true;
    return m.slice(0, DATA_URL_KEEP) + TRUNCATED;
  });
  text = text.replace(B64_FIELD, (_m, a: string, data: string, z: string) => {
    truncated = true;
    return a + data.slice(0, DATA_URL_KEEP) + TRUNCATED + z;
  });
  return { text, truncated };
}

export function summarizeResponse(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > SUMMARY_MAX ? flat.slice(0, SUMMARY_MAX) : flat;
}

function isEntry(v: unknown): v is HistoryEntry {
  if (typeof v !== 'object' || v === null) return false;
  const e = v as Record<string, unknown>;
  return typeof e.id === 'string' && typeof e.ts === 'number' && typeof e.method === 'string' && typeof e.path === 'string';
}

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadHistory(store: Storage | null = storage()): HistoryEntry[] {
  try {
    const raw = store?.getItem(HISTORY_KEY);
    if (!raw) return [];
    const list = JSON.parse(raw) as unknown;
    return Array.isArray(list) ? list.filter(isEntry).slice(0, HISTORY_MAX) : [];
  } catch {
    return [];
  }
}

export function saveHistory(list: readonly HistoryEntry[], store: Storage | null = storage()): void {
  try {
    store?.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_MAX)));
  } catch {
    /* quota or private mode: history is a convenience */
  }
}

let counter = 0;

/** Newest first, capped at 50. */
export function addEntry(list: readonly HistoryEntry[], entry: Omit<HistoryEntry, 'id' | 'body' | 'truncated'> & { body: string | null }): HistoryEntry[] {
  const cut = entry.body === null ? { text: null, truncated: false } : truncateDataUrls(entry.body);
  const item: HistoryEntry = {
    ...entry,
    id: `${entry.ts.toString(36)}-${(counter++).toString(36)}`,
    body: cut.text,
    response_summary: summarizeResponse(entry.response_summary),
  };
  if (cut.truncated) item.truncated = true;
  return [item, ...list].slice(0, HISTORY_MAX);
}

export function removeEntry(list: readonly HistoryEntry[], id: string): HistoryEntry[] {
  return list.filter((e) => e.id !== id);
}
