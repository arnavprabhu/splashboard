/**
 * Chat page logic (docs/ui/07): the side panel's fields and validation, the request body,
 * conversation grouping, titles, attachment limits, tool and schema checks, and the error
 * table (§10). Pure functions, unit tested in tests/chat.test.ts.
 */
import { ApiError } from '../../api/client';
import { t } from '../../strings/chat';
import type { ChatSummary, ModelEntry } from './types';

// ---------- sampling panel (§9.1) ----------

export const REASONING_EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const;
export const PRIORITIES = ['normal', 'foreground', 'background'] as const;

export type NumField =
  | 'temperature'
  | 'top_p'
  | 'top_k'
  | 'min_p'
  | 'presence_penalty'
  | 'frequency_penalty'
  | 'repetition_penalty'
  | 'seed'
  | 'max_completion_tokens'
  | 'timeout';

export const NUM_FIELDS: readonly NumField[] = [
  'temperature',
  'top_p',
  'top_k',
  'min_p',
  'presence_penalty',
  'frequency_penalty',
  'repetition_penalty',
  'seed',
  'max_completion_tokens',
  'timeout',
];

/** The panel as typed (strings, so "model default" = empty). */
export interface SamplingForm {
  reasoning_effort: string;
  num: Record<NumField, string>;
  stop: string[];
  priority: string;
  ignore_eos: boolean;
  kwargs: string;
}

export function emptySampling(): SamplingForm {
  return {
    reasoning_effort: '',
    num: Object.fromEntries(NUM_FIELDS.map((k) => [k, ''])) as Record<NumField, string>,
    stop: [],
    priority: '',
    ignore_eos: false,
    kwargs: '',
  };
}

/** Reads a stored `chat.sampling` object back into the form. */
export function samplingForm(stored: Record<string, unknown> | null | undefined): SamplingForm {
  const f = emptySampling();
  if (!stored) return f;
  if (typeof stored.reasoning_effort === 'string') f.reasoning_effort = stored.reasoning_effort;
  for (const k of NUM_FIELDS) {
    const v = stored[k] ?? (k === 'max_completion_tokens' ? stored.max_tokens : undefined);
    if (typeof v === 'number' && Number.isFinite(v)) f.num[k] = String(v);
  }
  if (Array.isArray(stored.stop)) f.stop = stored.stop.filter((s): s is string => typeof s === 'string');
  else if (typeof stored.stop === 'string') f.stop = [stored.stop];
  if (typeof stored.priority === 'string') f.priority = stored.priority;
  if (stored.ignore_eos === true) f.ignore_eos = true;
  if (stored.chat_template_kwargs && typeof stored.chat_template_kwargs === 'object') {
    f.kwargs = JSON.stringify(stored.chat_template_kwargs, null, 2);
  }
  return f;
}

const isInt = (n: number) => Number.isInteger(n);

/** One field's error in the copy of docs/ui/07 §9.1, or null. Empty is always fine. */
export function numError(field: NumField, raw: string, context?: number | null): string | null {
  const text = raw.trim();
  if (!text) return null;
  const n = Number(text);
  if (!Number.isFinite(n)) return t(`chat.err.${field === 'max_completion_tokens' ? 'max_tokens' : field}` as 'chat.err.temperature');
  switch (field) {
    case 'temperature':
      return n >= 0 && n <= 2 ? null : t('chat.err.temperature');
    case 'top_p':
      return n > 0 && n <= 1 ? null : t('chat.err.top_p');
    case 'top_k':
      return isInt(n) && n >= -1 ? null : t('chat.err.top_k');
    case 'min_p':
      return n >= 0 && n <= 1 ? null : t('chat.err.min_p');
    case 'presence_penalty':
      return n >= -2 && n <= 2 ? null : t('chat.err.presence_penalty');
    case 'frequency_penalty':
      return n >= -2 && n <= 2 ? null : t('chat.err.frequency_penalty');
    case 'repetition_penalty':
      return n > 0 ? null : t('chat.err.repetition_penalty');
    case 'seed':
      return isInt(n) && n >= 0 ? null : t('chat.err.seed');
    case 'max_completion_tokens':
      if (!isInt(n) || n < 1) return t('chat.err.max_tokens');
      return context && n > context ? t('chat.err.max_tokens_ctx', { n: context }) : null;
    case 'timeout':
      return n > 0 ? null : t('chat.err.timeout');
  }
}

/** Splash accepts up to 4 stop sequences (help line `chat.help.stop`). */
export const MAX_STOP = 4;

export function stopError(stop: readonly string[]): string | null {
  if (stop.length > MAX_STOP) return t('chat.err.stop_count');
  if (stop.some((s) => s.length > 64)) return t('chat.err.stop_len');
  return null;
}

export function kwargsError(text: string): string | null {
  if (!text.trim()) return null;
  try {
    const v: unknown = JSON.parse(text);
    return v && typeof v === 'object' && !Array.isArray(v) ? null : t('chat.err.kwargs');
  } catch {
    return t('chat.err.kwargs');
  }
}

export interface SamplingErrors {
  num: Partial<Record<NumField, string>>;
  stop: string | null;
  kwargs: string | null;
}

export function samplingErrors(f: SamplingForm, context?: number | null): SamplingErrors {
  const num: Partial<Record<NumField, string>> = {};
  for (const k of NUM_FIELDS) {
    const e = numError(k, f.num[k], context);
    if (e) num[k] = e;
  }
  return { num, stop: stopError(f.stop), kwargs: kwargsError(f.kwargs) };
}

export function hasSamplingErrors(e: SamplingErrors): boolean {
  return Object.keys(e.num).length > 0 || !!e.stop || !!e.kwargs;
}

/** Only the fields the user set; an untouched panel sends nothing (§9.1 "Model default"). */
export function samplingBody(f: SamplingForm, opts: { allowIgnoreEos: boolean }): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (f.reasoning_effort) out.reasoning_effort = f.reasoning_effort;
  for (const k of NUM_FIELDS) {
    const text = f.num[k].trim();
    if (text && Number.isFinite(Number(text))) out[k] = Number(text);
  }
  if (f.stop.length) out.stop = [...f.stop];
  if (f.priority && f.priority !== 'normal') out.priority = f.priority;
  if (f.ignore_eos && opts.allowIgnoreEos) out.ignore_eos = true;
  if (f.kwargs.trim()) {
    try {
      out.chat_template_kwargs = JSON.parse(f.kwargs);
    } catch {
      /* blocked by validation */
    }
  }
  return out;
}

// ---------- tools (§9.3) ----------

export interface ToolCheck {
  tools: Array<Record<string, unknown>>;
  names: string[];
  error: string | null;
}

/** "Line 7, column 3: …" for a JSON.parse failure when the engine gives a position. */
export function jsonErrorText(text: string, err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const m = /position (\d+)/i.exec(msg);
  if (m) {
    const pos = Number(m[1]);
    const before = text.slice(0, pos);
    const line = before.split('\n').length;
    const col = pos - before.lastIndexOf('\n');
    return t('chat.json.line', { line, col, msg: msg.replace(/ in JSON at position \d+.*$/i, '') });
  }
  return t('chat.json.invalid', { msg });
}

const TOOL_NAME = /^[A-Za-z0-9_-]{1,128}$/;

export function checkTools(text: string): ToolCheck {
  if (!text.trim()) return { tools: [], names: [], error: null };
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch (e) {
    return { tools: [], names: [], error: jsonErrorText(text, e) };
  }
  if (!Array.isArray(v)) return { tools: [], names: [], error: t('chat.json.not_array') };
  const names: string[] = [];
  for (let i = 0; i < v.length; i++) {
    const tool = v[i] as Record<string, unknown> | null;
    const n = i + 1;
    if (!tool || tool.type !== 'function') return { tools: [], names, error: t('chat.json.tool_type', { i: n }) };
    const fn = tool.function as Record<string, unknown> | undefined;
    const name = typeof fn?.name === 'string' ? fn.name : '';
    if (!TOOL_NAME.test(name)) return { tools: [], names, error: t('chat.json.tool_name', { i: n }) };
    if (fn?.parameters !== undefined && (typeof fn.parameters !== 'object' || fn.parameters === null || Array.isArray(fn.parameters)))
      return { tools: [], names, error: t('chat.json.tool_params', { i: n }) };
    if (names.includes(name)) return { tools: [], names, error: t('chat.json.tool_dup', { i: n, name }) };
    names.push(name);
  }
  return { tools: v as Array<Record<string, unknown>>, names, error: null };
}

export const TOOL_TEMPLATES: Record<'get_weather' | 'search' | 'run_sql' | 'empty', unknown[]> = {
  get_weather: [
    {
      type: 'function',
      function: {
        name: 'get_weather',
        description: 'Current weather for a city.',
        parameters: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] },
      },
    },
  ],
  search: [
    {
      type: 'function',
      function: {
        name: 'search',
        description: 'Search the web and return the top results.',
        parameters: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] },
      },
    },
  ],
  run_sql: [
    {
      type: 'function',
      function: {
        name: 'run_sql',
        description: 'Run a read-only SQL query.',
        parameters: { type: 'object', properties: { sql: { type: 'string' } }, required: ['sql'] },
      },
    },
  ],
  empty: [],
};

export type ToolChoiceKind = 'auto' | 'none' | 'required' | 'named';

export function toolChoiceBody(kind: ToolChoiceKind, named: string): unknown {
  if (kind === 'named') return { type: 'function', function: { name: named } };
  return kind;
}

export function toolChoiceError(kind: ToolChoiceKind, count: number): string | null {
  return (kind === 'required' || kind === 'named') && count === 0 ? t('chat.tools.need_tool') : null;
}

// ---------- output (§9.4) ----------

export type OutputKind = 'text' | 'json_object' | 'json_schema';

export interface OutputForm {
  kind: OutputKind;
  schema: string;
  name: string;
  strict: boolean;
}

export const OUTPUT_TEMPLATES: Record<'person' | 'list' | 'classification', Record<string, unknown>> = {
  person: { type: 'object', properties: { name: { type: 'string' }, age: { type: 'integer' } }, required: ['name'] },
  list: { type: 'object', properties: { items: { type: 'array', items: { type: 'string' } } }, required: ['items'] },
  classification: {
    type: 'object',
    properties: { label: { type: 'string', enum: ['positive', 'negative', 'neutral'] }, confidence: { type: 'number' } },
    required: ['label'],
  },
};

export function schemaError(text: string): string | null {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch (e) {
    return jsonErrorText(text, e);
  }
  if (!v || typeof v !== 'object' || Array.isArray(v)) return t('chat.output.schema_object');
  const s = v as Record<string, unknown>;
  if (!('type' in s)) return t('chat.output.schema_type');
  if (s.type === 'object' && (typeof s.properties !== 'object' || s.properties === null)) return t('chat.output.schema_properties');
  return null;
}

export function outputError(f: OutputForm): string | null {
  if (f.kind !== 'json_schema') return null;
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(f.name)) return t('chat.output.name_invalid');
  return schemaError(f.schema);
}

export function outputBody(f: OutputForm): Record<string, unknown> | null {
  if (f.kind === 'json_object') return { type: 'json_object' };
  if (f.kind === 'json_schema') {
    try {
      return { type: 'json_schema', json_schema: { name: f.name, schema: JSON.parse(f.schema), strict: f.strict } };
    } catch {
      return null;
    }
  }
  return null;
}

export function outputForm(stored: Record<string, unknown> | null | undefined): OutputForm {
  const base: OutputForm = { kind: 'text', schema: JSON.stringify(OUTPUT_TEMPLATES.person, null, 2), name: 'answer', strict: true };
  if (!stored) return base;
  if (stored.type === 'json_object') return { ...base, kind: 'json_object' };
  if (stored.type === 'json_schema') {
    const js = (stored.json_schema ?? {}) as Record<string, unknown>;
    return {
      kind: 'json_schema',
      schema: JSON.stringify(js.schema ?? {}, null, 2),
      name: typeof js.name === 'string' ? js.name : 'answer',
      strict: js.strict !== false,
    };
  }
  return base;
}

/** The result line for a reply that ran with a response format (§9.4). */
export function checkResult(text: string, format: Record<string, unknown> | null | undefined, stopped: boolean): { ok: boolean; line: string } {
  let v: unknown;
  try {
    v = JSON.parse(text);
  } catch {
    return { ok: false, line: t('chat.output.result_invalid', { reason: stopped ? t('chat.output.reason_stopped') : t('chat.output.reason_parse') }) };
  }
  if (format?.type !== 'json_schema') return { ok: true, line: t('chat.output.result_valid') };
  const js = (format.json_schema ?? {}) as { name?: string; schema?: Record<string, unknown> };
  const schema = js.schema ?? {};
  if (schema.type === 'object') {
    if (!v || typeof v !== 'object' || Array.isArray(v)) return { ok: false, line: t('chat.output.result_invalid', { reason: t('chat.output.reason_type', { type: 'object' }) }) };
    const required = Array.isArray(schema.required) ? (schema.required as unknown[]).filter((k): k is string => typeof k === 'string') : [];
    const missing = required.find((k) => !(k in (v as Record<string, unknown>)));
    if (missing) return { ok: false, line: t('chat.output.result_invalid', { reason: t('chat.output.reason_required', { key: missing }) }) };
  }
  return { ok: true, line: t('chat.output.result_matches', { name: js.name ?? 'answer' }) };
}

// ---------- conversation list (§3) ----------

export type Group = 'today' | 'yesterday' | 'week' | 'month' | 'older';
export const GROUPS: readonly Group[] = ['today', 'yesterday', 'week', 'month', 'older'];

const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

export function groupOf(iso: string, now: Date = new Date()): Group {
  const t0 = Date.parse(iso);
  if (!Number.isFinite(t0)) return 'older';
  const today = dayStart(now);
  if (t0 >= today) return 'today';
  if (t0 >= today - 86_400_000) return 'yesterday';
  if (t0 >= today - 7 * 86_400_000) return 'week';
  const d = new Date(t0);
  if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()) return 'month';
  return 'older';
}

export function groupChats(chats: readonly ChatSummary[], now: Date = new Date()): Array<{ group: Group; chats: ChatSummary[] }> {
  const sorted = [...chats].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  return GROUPS.map((group) => ({ group, chats: sorted.filter((c) => groupOf(c.updated_at, now) === group) })).filter((g) => g.chats.length > 0);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** `HH:MM` today, "Yesterday", weekday within 7 days, `D MMM`, `D MMM YYYY` across years (§3.1). */
export function listTime(iso: string, now: Date = new Date()): string {
  const t0 = Date.parse(iso);
  if (!Number.isFinite(t0)) return '';
  const d = new Date(t0);
  const g = groupOf(iso, now);
  const pad = (n: number) => String(n).padStart(2, '0');
  if (g === 'today') return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (g === 'yesterday') return t('chat.list.yesterday');
  if (g === 'week') return WEEKDAYS[d.getDay()]!;
  return d.getFullYear() === now.getFullYear() ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

/** First 60 characters of the first user message, cut at a word, no trailing punctuation (§3.3). */
export function autoTitle(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  if (!flat) return '';
  if (flat.length <= 60) return flat.replace(/[\s.,;:!?…-]+$/, '');
  const cut = flat.slice(0, 60);
  const space = cut.lastIndexOf(' ');
  return (space > 20 ? cut.slice(0, space) : cut).replace(/[\s.,;:!?…-]+$/, '');
}

// ---------- model selector (§4) ----------

/** The repo part after the owner, for the composer placeholder (§7.1). */
export function shortModel(id: string | null | undefined): string {
  if (!id) return '';
  const slash = id.indexOf('/');
  return slash >= 0 ? id.slice(slash + 1) : id;
}

export function splitModel(value: string): { model: string; profile: string } {
  const m = /^(.*?):([a-z0-9][a-z0-9_-]{0,31})$/.exec(value);
  return m && m[1]!.includes('/') && !/[A-Z]/.test(m[2]!) ? { model: m[1]!, profile: m[2]! } : { model: value, profile: 'default' };
}

/** Request `model` for a model and profile (§7.5: the default profile is the bare id). */
export function requestModel(model: string, profile: string | null | undefined): string {
  return profile && profile !== 'default' ? `${model}:${profile}` : model;
}

export interface ModelRow {
  id: string;
  active: boolean;
  context: number | null;
  estimated: boolean;
  vision: boolean | null;
  profiles: string[];
}

/** Rows for the selector from `/v1/models` (active first, then installed). */
export function modelRows(entries: readonly ModelEntry[]): ModelRow[] {
  const rows: ModelRow[] = [];
  const byId = new Map<string, ModelRow>();
  for (const e of entries) {
    if (e.profile && e.root) {
      const row = byId.get(e.root);
      if (row && !row.profiles.includes(e.profile)) row.profiles.push(e.profile);
      continue;
    }
    const ctx = e.max_model_len ?? e.context_length ?? null;
    const row: ModelRow = {
      id: e.id,
      active: e.loaded === true,
      context: ctx ?? null,
      estimated: e.loaded !== true,
      vision: typeof e.vision === 'boolean' ? e.vision : Array.isArray(e.input_modalities) ? e.input_modalities.includes('image') : null,
      profiles: [],
    };
    if (!byId.has(e.id)) {
      byId.set(e.id, row);
      rows.push(row);
    }
  }
  return rows;
}

// ---------- attachments (§7.3) ----------

export const MAX_PAGES = 64;
export const MAX_BYTES = 64 * 1024 * 1024;
export const DEFAULT_MAX_IMAGE_PIXELS = 4_194_304;

/** Counts `/Type /Page` objects (not `/Pages`); null when none are found. */
export function countPdfPages(bytes: Uint8Array): number | null {
  let text = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) text += String.fromCharCode(...bytes.subarray(i, i + chunk));
  const matches = text.match(/\/Type\s*\/Page(?![s\w])/g);
  return matches && matches.length > 0 ? matches.length : null;
}

export function attachmentError(items: ReadonlyArray<{ bytes: number; pages: number | null; kind: string }>): string | null {
  const pages = items.reduce((n, a) => n + (a.kind === 'pdf' ? (a.pages ?? 0) : 0), 0);
  if (pages > MAX_PAGES) return t('chat.att.too_many_pages', { n: pages });
  const bytes = items.reduce((n, a) => n + a.bytes, 0);
  if (bytes > MAX_BYTES) return t('chat.att.too_big', { size: (bytes / 1024 / 1024).toFixed(1) });
  return null;
}

// ---------- errors (§10) ----------

export type ErrorKind =
  | 'unreachable'
  | 'busy'
  | 'recovering'
  | 'failed'
  | 'capacity'
  | 'resource_timeout'
  | 'request_timeout'
  | 'queue_full'
  | 'mask_timeout'
  | 'later_system'
  | 'not_found'
  | 'attachment'
  | 'ignore_eos'
  | 'rejected'
  | 'sign_in'
  | 'generic';

export interface ChatError {
  kind: ErrorKind;
  title: string;
  body: string | null;
  /** Engine/manager text shown verbatim. */
  detail: string | null;
  retryAfter: number | null;
  status: number | null;
}

const LATER_SYSTEM = /does not accept system messages after the first/i;

export function classifyError(err: unknown, ctx: { model?: string | null; active?: string | null } = {}): ChatError {
  const base = { detail: null as string | null, retryAfter: null as number | null, status: null as number | null };
  if (!(err instanceof ApiError)) {
    const msg = err instanceof Error ? err.message : String(err);
    if (err instanceof TypeError || /network|fetch/i.test(msg)) return { ...base, kind: 'unreachable', title: t('chat.error.unreachable'), body: t('chat.error.unreachable_body') };
    return { ...base, kind: 'generic', title: t('chat.error.generic'), body: null, detail: msg };
  }
  const code = err.code ?? '';
  const status = err.status;
  const common = { detail: err.message, retryAfter: err.retryAfter, status };
  if (status === 0) return { ...common, kind: 'unreachable', title: t('chat.error.unreachable'), body: t('chat.error.unreachable_body') };
  if (code === 'model_switch_busy') return { ...common, kind: 'busy', title: t('chat.error.busy', { model: shortModel(ctx.active) || ctx.active || '—' }), body: t('chat.error.busy_body') };
  if (code === 'engine_recovering' || code === 'engine_unavailable') return { ...common, kind: 'recovering', title: t('chat.error.recovering'), body: t('chat.error.not_sent') };
  if (code === 'engine_failed') return { ...common, kind: 'failed', title: t('chat.error.failed'), body: null };
  if (code === 'capacity_exhausted') return { ...common, kind: 'capacity', title: t('chat.error.capacity'), body: t('chat.error.capacity_body') };
  if (code === 'resource_timeout') return { ...common, kind: 'resource_timeout', title: t('chat.error.resource_timeout'), body: t('chat.error.resource_timeout_body') };
  if (status === 504 || code === 'request_timeout') return { ...common, kind: 'request_timeout', title: t('chat.error.request_timeout'), body: null };
  if (code === 'frontend_overloaded') return { ...common, kind: 'queue_full', title: t('chat.error.queue_full'), body: t('chat.error.queue_full_body') };
  if (code === 'mask_timeout') return { ...common, kind: 'mask_timeout', title: t('chat.error.mask_timeout'), body: t('chat.error.mask_timeout_body') };
  if (code === 'model_not_found' || (status === 404 && /model/i.test(err.message)))
    return { ...common, kind: 'not_found', title: t('chat.error.not_found', { model: ctx.model ?? '—' }), body: t('chat.error.not_found_body') };
  if (status === 400 && LATER_SYSTEM.test(err.message)) return { ...common, kind: 'later_system', title: t('chat.error.later_system'), body: t('chat.error.later_system_body') };
  if (status === 400 && /ignore_eos/i.test(err.message)) return { ...common, kind: 'ignore_eos', title: t('chat.error.ignore_eos'), body: null };
  if (status === 413 || (status === 400 && /image|pdf|attachment|file/i.test(err.message)))
    return { ...common, kind: 'attachment', title: t('chat.error.attachment'), body: null };
  // /v1 with "Require API key" on and admin sign-in off: the page has no session yet (D58).
  if (status === 401) return { ...common, kind: 'sign_in', title: t('chat.error.sign_in'), body: t('chat.error.sign_in_body') };
  if (status >= 400 && status < 500) return { ...common, kind: 'rejected', title: t('chat.error.rejected'), body: null };
  return { ...common, kind: 'generic', title: t('chat.error.generic'), body: null };
}

/** Kinds that retry by themselves after `Retry-After` (D-07-11: at most 3 times). */
export function autoRetry(kind: ErrorKind): boolean {
  return kind === 'busy' || kind === 'recovering' || kind === 'queue_full';
}

export const MAX_AUTO_RETRIES = 3;
