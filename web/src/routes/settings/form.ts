/**
 * Pure settings form model. No Preact here so it can be
 * unit-tested: field references and their document paths, edits applied over a base document,
 * dirty tracking, issue → field mapping, section slugs ↔ schema section ids.
 *
 * The draft is a map of edits over the last saved document rather than a copy of it, so a
 * `settings.changed` event from another client rebases the draft for free (only the user's
 * own edits stay dirty).
 */

import type { SettingsDoc } from '../../api/types';

/** A setting in the global block (`model = null`) or in one model's overrides. */
export interface FieldRef {
  key: string;
  model: string | null;
}

/** Marks an edit that deletes the key (per-model "Reset to global"). */
export const UNSET: unique symbol = Symbol('unset');
export type EditValue = unknown | typeof UNSET;

export interface Edit {
  ref: FieldRef;
  value: EditValue;
}

export type Edits = ReadonlyMap<string, Edit>;

/** Stable id for a field reference: `*|serve.max_context` or `<model>|serve.max_context`. */
export function refId(ref: FieldRef): string {
  return `${ref.model ?? '*'}|${ref.key}`;
}

/** Path of a field in the settings document. */
export function refPath(ref: FieldRef): string[] {
  const parts = ref.key.split('.');
  return ref.model ? ['models', ref.model, ...parts] : ['global', ...parts];
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

export function getAt(doc: unknown, path: readonly (string | number)[]): unknown {
  let node: unknown = doc;
  for (const part of path) {
    if (Array.isArray(node) && typeof part === 'number') node = node[part];
    else if (isRecord(node)) node = node[String(part)];
    else return undefined;
  }
  return node;
}

/** Sets (or, with UNSET, deletes) a value in place, creating intermediate objects. */
function setAtMut(doc: Record<string, unknown>, path: readonly string[], value: EditValue): void {
  let node = doc;
  for (let i = 0; i < path.length - 1; i += 1) {
    const part = path[i]!;
    const next = node[part];
    if (!isRecord(next)) {
      if (value === UNSET) return;
      node[part] = {};
    }
    node = node[part] as Record<string, unknown>;
  }
  const last = path[path.length - 1]!;
  if (value === UNSET) delete node[last];
  else node[last] = value;
}

export function clone<T>(value: T): T {
  return value === undefined ? value : (JSON.parse(JSON.stringify(value)) as T);
}

/** Deep structural equality for JSON values. Object key order does not matter. */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a === 'number' && typeof b === 'number') return Number.isNaN(a) && Number.isNaN(b);
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((x, i) => deepEqual(x, b[i]));
  }
  if (isRecord(a) && isRecord(b)) {
    const ka = Object.keys(a).filter((k) => a[k] !== undefined);
    const kb = Object.keys(b).filter((k) => b[k] !== undefined);
    if (ka.length !== kb.length) return false;
    return ka.every((k) => deepEqual(a[k], b[k]));
  }
  return false;
}

/** The document the user would save: base with every edit applied. */
export function applyEdits(base: SettingsDoc, edits: Edits): SettingsDoc {
  const doc = clone(base) as unknown as Record<string, unknown>;
  for (const { ref, value } of edits.values()) setAtMut(doc, refPath(ref), value);
  return doc as unknown as SettingsDoc;
}

/** The stored value of a field in a document (undefined when absent). */
export function storedValue(doc: SettingsDoc | null | undefined, ref: FieldRef): unknown {
  return doc ? getAt(doc, refPath(ref)) : undefined;
}

/** Whether an edit changes the base document. */
export function isDirtyEdit(base: SettingsDoc, edit: Edit): boolean {
  const before = storedValue(base, edit.ref);
  if (edit.value === UNSET) return before !== undefined;
  return !deepEqual(before, edit.value);
}

/** Edits that differ from the base, i.e. the unsaved changes. */
export function dirtyEdits(base: SettingsDoc | null, edits: Edits): Edit[] {
  if (!base) return [];
  return [...edits.values()].filter((e) => isDirtyEdit(base, e));
}

/** Returns a new edits map with `ref` set to `value` (dropping the edit when it equals the base). */
export function withEdit(base: SettingsDoc | null, edits: Edits, ref: FieldRef, value: EditValue): Map<string, Edit> {
  const next = new Map(edits);
  const edit: Edit = { ref, value };
  if (base && !isDirtyEdit(base, edit)) next.delete(refId(ref));
  else next.set(refId(ref), edit);
  return next;
}

/** The value a field shows: the edit when there is one, else the stored value. */
export function draftValue(base: SettingsDoc | null, edits: Edits, ref: FieldRef): unknown {
  const edit = edits.get(refId(ref));
  if (edit) return edit.value === UNSET ? undefined : edit.value;
  return storedValue(base, ref);
}

// ---------- issues ----------

/** One validation problem as the manager reports it. */
export interface Issue {
  path?: ReadonlyArray<string | number>;
  key?: string;
  model?: string | null;
  message: string;
  severity?: 'error' | 'warning' | string;
  code?: string;
}

export interface FieldIssues {
  errors: string[];
  warnings: string[];
}

export interface MappedIssues {
  /** By refId. */
  byField: Map<string, FieldIssues>;
  /** `engine.extra_flags` row problems by row index. */
  extraFlags: Map<number, string[]>;
  /** Issues that do not belong to a known field. */
  other: Issue[];
}

/**
 * Field key for an issue: the metadata `key` when it is a known field, else the dotted path
 * after `global` / `models.<id>`. `engine.extra_flags.0.flag` belongs to `engine.extra_flags`.
 */
export function issueRef(issue: Issue, knownKeys: ReadonlySet<string>): FieldRef | null {
  const model = issue.model ?? (issue.path?.[0] === 'models' && typeof issue.path[1] === 'string' ? issue.path[1] : null);
  const candidates: string[] = [];
  if (issue.key) candidates.push(issue.key);
  const path = issue.path ?? [];
  const rest = path[0] === 'global' ? path.slice(1) : path[0] === 'models' ? path.slice(2) : path;
  const dotted = rest.filter((p) => typeof p === 'string').join('.');
  if (dotted) candidates.push(dotted);
  for (const c of candidates) {
    if (knownKeys.has(c)) return { key: c, model };
    // Longest known prefix: engine.extra_flags.0.flag → engine.extra_flags.
    const parts = c.split('.');
    for (let n = parts.length - 1; n > 0; n -= 1) {
      const prefix = parts.slice(0, n).join('.');
      if (knownKeys.has(prefix)) return { key: prefix, model };
    }
  }
  return null;
}

/** Index of an `engine.extra_flags` row from an issue path, or null. */
export function extraFlagIndex(issue: Issue): number | null {
  const path = issue.path ?? [];
  const i = path.indexOf('extra_flags');
  const n = i >= 0 ? path[i + 1] : undefined;
  if (typeof n === 'number') return n;
  const m = /extra_flags\.(\d+)/.exec(issue.key ?? '');
  return m ? Number(m[1]) : null;
}

export function mapIssues(issues: readonly Issue[], knownKeys: ReadonlySet<string>): MappedIssues {
  const byField = new Map<string, FieldIssues>();
  const extraFlags = new Map<number, string[]>();
  const other: Issue[] = [];
  for (const issue of issues) {
    const ref = issueRef(issue, knownKeys);
    if (!ref) {
      other.push(issue);
      continue;
    }
    const id = refId(ref);
    const entry = byField.get(id) ?? { errors: [], warnings: [] };
    const list = issue.severity === 'warning' ? entry.warnings : entry.errors;
    if (!list.includes(issue.message)) list.push(issue.message);
    byField.set(id, entry);
    if (ref.key === 'engine.extra_flags' && issue.severity !== 'warning') {
      const row = extraFlagIndex(issue);
      if (row !== null) extraFlags.set(row, [...(extraFlags.get(row) ?? []), issue.message]);
    }
  }
  return { byField, extraFlags, other };
}

// ---------- sections ----------

/** URL slug (web/src/routes/tabs.ts) → schema section id. */
export const SLUG_TO_SECTION: Readonly<Record<string, string>> = {
  server: 'server_network',
  security: 'security',
  storage: 'models_storage',
  memory: 'memory_context',
  cache: 'cache',
  performance: 'performance',
  requests: 'requests_limits',
  sampling: 'reasoning_sampling',
  routing: 'routing',
  hf: 'hugging_face',
  chat: 'chat_mcp',
  lifecycle: 'lifecycle',
  menubar: 'menu_bar',
  notifications: 'notifications',
  data: 'data_privacy',
  advanced: 'advanced',
  about: 'about',
};

export const SECTION_TO_SLUG: Readonly<Record<string, string>> = Object.fromEntries(
  Object.entries(SLUG_TO_SECTION).map(([slug, id]) => [id, slug]),
);

/** Keys the global page renders with dedicated controls instead of the generic field. */
export const CUSTOM_KEYS: ReadonlySet<string> = new Set(['chat.mcp_servers', 'engine.extra_flags', 'ui.theme']);

// ---------- save planning ----------

export type Applies = 'immediate' | 'restart' | 'next_load';

export interface SavePlan {
  changes: number;
  /** Any dirty field that needs an engine restart (the engine must be running to matter). */
  restart: boolean;
  /** server.host or server.port changed: the manager moves. */
  rebind: { host: string; port: number } | null;
}

export function planSave(
  base: SettingsDoc | null,
  edits: Edits,
  appliesOf: (key: string) => Applies | undefined,
): SavePlan {
  const dirty = dirtyEdits(base, edits);
  const restart = dirty.some((e) => appliesOf(e.ref.key) === 'restart');
  let rebind: SavePlan['rebind'] = null;
  if (base && dirty.some((e) => e.ref.model === null && (e.ref.key === 'server.host' || e.ref.key === 'server.port'))) {
    const doc = applyEdits(base, edits);
    const host = String(getAt(doc, ['global', 'server', 'host']) ?? '127.0.0.1');
    const port = Number(getAt(doc, ['global', 'server', 'port']) ?? 8000);
    rebind = { host, port };
  }
  return { changes: dirty.length, restart, rebind };
}

/** The URL the admin moves to after a rebind. A wildcard bind keeps the current host name. */
export function rebindUrl(target: { host: string; port: number }, current: { hostname: string; protocol: string }, path: string): string {
  const wildcard = target.host === '0.0.0.0' || target.host === '::' || target.host === '';
  const host = wildcard ? current.hostname : target.host;
  const authority = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
  return `${current.protocol}//${authority}:${target.port}${path}`;
}

export function isLoopback(host: string | null | undefined): boolean {
  if (!host) return true;
  return host === '127.0.0.1' || host === 'localhost' || host === '::1' || host.startsWith('127.');
}

/** Engine states in which a restart-requiring change matters now. */
const RUNNING: ReadonlySet<string> = new Set(['ready', 'busy', 'idle_released', 'recovering', 'engine_failed']);

export function engineRunning(state: string | null | undefined): boolean {
  if (!state) return false;
  return RUNNING.has(state) || state.startsWith('starting');
}
