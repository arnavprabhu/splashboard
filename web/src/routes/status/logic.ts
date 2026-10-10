/**
 * Pure helpers for the Status page. No DOM, no fetches: everything here is unit
 * tested in tests/status-logic.test.ts.
 */

import type { EngineError, EngineNotice, EngineSuggestion, EngineView, InstalledModel } from '../../api/models';
import type { EngineState, SettingsDoc } from '../../api/types';

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

/** The full EngineView behind the store's summary (fields may be missing on older managers). */
export function viewOf(view: Record<string, unknown> | null | undefined): Partial<EngineView> {
  return (view ?? {}) as Partial<EngineView>;
}

/** Repo short name: owner dropped, variant kept. */
export function shortName(model: string | null | undefined): string {
  if (!model) return '';
  const slash = model.indexOf('/');
  return slash < 0 ? model : model.slice(slash + 1);
}

// ---------- header actions per state ----------

export type HeaderAction = 'stop' | 'restart' | 'switch' | 'load' | 'open_downloader' | 'retry';

export interface ActionSpec {
  id: HeaderAction;
  accent?: boolean;
  disabled?: boolean;
}

/**
 * Which buttons the header shows. `hasModels` is null while the installed list is unknown.
 * Only one action is ever accent.
 */
export function headerActions(state: EngineState | null, hasModels: boolean | null): ActionSpec[] {
  switch (state) {
    case null:
      return [];
    case 'stopped':
      return hasModels ? [{ id: 'load', accent: true }] : [{ id: 'open_downloader', accent: true }];
    case 'starting.installing':
    case 'starting.loading':
    case 'starting.warming':
    case 'crashed':
      return [{ id: 'stop' }];
    case 'ready':
    case 'busy':
    case 'idle_released':
      return [{ id: 'stop' }, { id: 'restart' }, { id: 'switch' }];
    case 'recovering':
      return [{ id: 'stop' }, { id: 'restart' }];
    case 'engine_failed':
      return [{ id: 'restart', accent: true }, { id: 'stop' }];
    case 'stopping':
      return [
        { id: 'stop', disabled: true },
        { id: 'restart', disabled: true },
        { id: 'switch', disabled: true },
      ];
    case 'failed':
      return [{ id: 'retry', accent: true }, { id: 'switch' }];
  }
}

/** Stop/Restart/Switch ask first only while requests are in flight. */
export function needsConfirm(action: HeaderAction, inFlight: number): boolean {
  return inFlight > 0 && (action === 'stop' || action === 'restart' || action === 'switch');
}

// ---------- which bands show ----------

export type StateGroup = 'stopped' | 'starting' | 'live' | 'frozen' | 'failed' | 'unknown';

export function stateGroup(state: EngineState | null): StateGroup {
  if (state === null) return 'unknown';
  if (state === 'stopped') return 'stopped';
  if (state.startsWith('starting.')) return 'starting';
  if (state === 'failed') return 'failed';
  if (state === 'ready' || state === 'busy' || state === 'idle_released') return 'live';
  return 'frozen'; // recovering, engine_failed, stopping, crashed
}

// ---------- header meta ----------

export interface MetaInput {
  format?: string | null;
  context?: number | null;
  kv?: string | null;
  vision?: boolean | null;
  draft?: string | null;
  uptime?: string | null;
  version?: string | null;
  starting?: boolean;
}

/** Ordered meta items; missing ones are dropped, except CONTEXT while starting (shows —). */
export function metaKeys(m: MetaInput): Array<'format' | 'context' | 'kv' | 'vision' | 'draft' | 'uptime' | 'version'> {
  const out: Array<'format' | 'context' | 'kv' | 'vision' | 'draft' | 'uptime' | 'version'> = [];
  if (m.format) out.push('format');
  if (m.context != null || m.starting) out.push('context');
  if (m.kv) out.push('kv');
  if (m.vision != null) out.push('vision');
  if (m.draft !== undefined) out.push('draft');
  if (m.uptime) out.push('uptime');
  if (m.version) out.push('version');
  return out;
}

export function formatLabel(m: Pick<InstalledModel, 'format' | 'variant'> | null | undefined): string | null {
  if (!m) return null;
  if (m.format === 'gguf') return m.variant ? `GGUF ${m.variant}` : 'GGUF';
  if (m.format === 'mlx') return 'MLX';
  if (m.format === 'legacy') return 'Splash package';
  return null;
}

// ---------- settings patches from engine suggestions ----------

/** Per-model `serve` keys (scope M and G/M). */
export const MODEL_SERVE_KEYS: ReadonlySet<string> = new Set([
  'revision',
  'draft_model',
  'language_only',
  'served_model_names',
  'announce_served_name',
  'default_reasoning_effort',
  'kv_format',
  'max_context',
  'decode_share',
  'max_image_pixels',
]);

/**
 * Applies a suggestion's `patch` (`{"serve.max_context": "64K"}`) to a settings document.
 * Keys a model may override go to `models[model].serve`; the rest are global.
 */
export function applyPatch(doc: SettingsDoc, patch: Record<string, unknown>, model: string | null): SettingsDoc {
  const next: SettingsDoc = JSON.parse(JSON.stringify(doc)) as SettingsDoc;
  next.global ??= {};
  next.models ??= {};
  for (const [key, value] of Object.entries(patch)) {
    const dot = key.indexOf('.');
    if (dot < 0) continue;
    const section = key.slice(0, dot);
    const field = key.slice(dot + 1);
    if (section === 'serve' && model && MODEL_SERVE_KEYS.has(field)) {
      const entry = (next.models[model] ??= {});
      const serve = isRecord(entry.serve) ? entry.serve : {};
      entry.serve = { ...serve, [field]: value };
    } else {
      const current = isRecord(next.global[section]) ? next.global[section] : {};
      next.global[section] = { ...current, [field]: value };
    }
  }
  return next;
}

export type SuggestionKind = 'apply_retry' | 'apply_restart' | 'navigate' | 'retry' | 'restart';

/** What a suggestion button does. Settings-writing fixes retry right away. */
export function suggestionKind(s: EngineSuggestion): SuggestionKind {
  switch (s.action) {
    case 'raise_max_memory':
    case 'smaller_variant':
    case 'add_hf_token':
    case 'open_logs':
      return 'navigate';
    case 'retry':
      return 'retry';
    case 'restart_engine':
      return 'restart';
    case 'enable_ssd_cache':
      return 'apply_restart';
    case 'go_offline':
      return 'apply_retry';
    default:
      return s.patch ? 'apply_retry' : 'retry';
  }
}

/** The patch a suggestion writes (go_offline has none in the API; it means `hf.offline`). */
export function suggestionPatch(s: EngineSuggestion): Record<string, unknown> | null {
  if (s.patch) return s.patch;
  if (s.action === 'go_offline') return { 'hf.offline': true };
  return null;
}

/** Admin route a navigate-suggestion opens. */
export function suggestionHref(s: EngineSuggestion, family?: string | null): string | null {
  switch (s.action) {
    case 'raise_max_memory':
      return '/settings/memory#serve.max_memory';
    case 'smaller_variant':
      return `/models/downloader?tab=supported${family ? `&family=${encodeURIComponent(family)}` : ''}`;
    case 'add_hf_token':
      return '/settings/hf';
    case 'open_logs':
      return '/logs?source=engine&level=error';
    default:
      return null;
  }
}

/**
 * Suggestions in display order with the context fix first (it is the one known to work, so it
 * is the accent button). Raise --max-memory only shows when a ceiling is set (memory capped).
 */
export function orderSuggestions(list: readonly EngineSuggestion[], maxMemory: unknown): EngineSuggestion[] {
  const capped = typeof maxMemory === 'string' ? maxMemory.trim().toLowerCase() !== 'auto' && maxMemory.trim() !== '' : maxMemory != null;
  const rank: Record<string, number> = { lower_max_context: 0, language_only: 1, raise_max_memory: 2, smaller_variant: 3 };
  return list
    .filter((s) => s.action !== 'raise_max_memory' || capped)
    .map((s, i) => ({ s, i }))
    .sort((a, b) => (rank[a.s.action] ?? 10) - (rank[b.s.action] ?? 10) || a.i - b.i)
    .map(({ s }) => s);
}

// ---------- budget refusal text ----------

export interface ContextRefusal {
  asked: number;
  allowed: number;
}

/** "--max-context N exceeds the M tokens …" (Bootstrap.mm) or the manager's rewording. */
export function parseContextRefusal(err: Pick<EngineError, 'message' | 'raw'> | null | undefined): ContextRefusal | null {
  if (!err) return null;
  const toInt = (s: string) => Number(s.replace(/,/g, ''));
  for (const line of err.raw ?? []) {
    const m = /--max-context ([\d,]+) exceeds the ([\d,]+) tokens/.exec(line);
    if (m?.[1] && m[2]) return { asked: toInt(m[1]), allowed: toInt(m[2]) };
  }
  const m = /--max-context ([\d,]+) (?:is more than|exceeds) the ([\d,]+) tokens/.exec(err.message ?? '');
  return m?.[1] && m[2] ? { asked: toInt(m[1]), allowed: toInt(m[2]) } : null;
}

/** Bytes of vision weights in the breakdown, for the language-only button's note. */
export function visionBytes(budget: EngineError['budget']): number | null {
  const row = (budget ?? []).find((r) => /vision/i.test(r.label));
  return row?.bytes ?? null;
}

// ---------- disk-tier suggestion ----------

export interface DiskSuggestion {
  availableBytes: number | null;
  tokens: number | null;
}

export function diskNotice(notices: readonly EngineNotice[] | null | undefined): EngineNotice | null {
  return (notices ?? []).find((n) => n.kind === 'disk_tier_suggestion') ?? null;
}

/** "The {N} MiB this Mac had available at startup may not hold a {C}-token request; …" */
export function parseDiskSuggestion(text: string | null | undefined): DiskSuggestion {
  const m = /The ([\d,]+) MiB this Mac had available at startup may not hold an? ([\d,]+)-token request/.exec(text ?? '');
  if (!m?.[1] || !m[2]) return { availableBytes: null, tokens: null };
  return { availableBytes: Number(m[1].replace(/,/g, '')) * 1024 * 1024, tokens: Number(m[2].replace(/,/g, '')) };
}

/** Decision T4: 16G, or 32G when the persistent cache is on (the Coding preset's value). */
export function ssdCacheSize(persistent: unknown): '16G' | '32G' {
  return persistent === true ? '32G' : '16G';
}

// ---------- endpoints ----------

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);

export function isLoopback(host: string | null | undefined): boolean {
  return !host || LOOPBACK.has(host);
}

export function endpoints(origin: string): { openai: string; anthropic: string } {
  const base = origin.replace(/\/$/, '');
  return { openai: `${base}/v1`, anthropic: base };
}

/** LAN address when the manager binds beyond loopback: `http://{hostname}.local:{port}`. */
export function lanUrl(host: string | null | undefined, hostname: string | null | undefined, port: number | null | undefined): string | null {
  if (isLoopback(host) || !port) return null;
  const name = host === '0.0.0.0' || host === '::' ? (hostname ? `${hostname.replace(/\.local$/, '')}.local` : null) : host;
  return name ? `http://${name}:${port}` : null;
}

// ---------- Claude Code (splash/install/clients.py `_claude`) ----------

export interface ClaudeEnvInput {
  baseUrl: string;
  model: string;
  /** null = the key is not required ("local", as clients.py sends without SPLASH_API_KEY). */
  key: string | null;
  context: number | null;
}

export function launchCommand(model: string | null, defaultModel: string | null): string {
  return model && model !== defaultModel ? `splash launch claude --model ${model}` : 'splash launch claude';
}

/** The session-only environment Splash's launcher sets for Claude Code, as shell. */
export function claudeEnv({ baseUrl, model, key, context }: ClaudeEnvInput): string {
  const lines = [
    `export ANTHROPIC_BASE_URL=${baseUrl}`,
    `export ANTHROPIC_AUTH_TOKEN=${key ?? 'local'}`,
    `export ANTHROPIC_MODEL=${model}`,
    `export ANTHROPIC_DEFAULT_OPUS_MODEL=${model}`,
    `export ANTHROPIC_DEFAULT_SONNET_MODEL=${model}`,
    `export ANTHROPIC_DEFAULT_HAIKU_MODEL=${model}`,
    `export ANTHROPIC_SMALL_FAST_MODEL=${model}`,
  ];
  if (context && context > 0) {
    lines.push(`export CLAUDE_CODE_MAX_CONTEXT_TOKENS=${context}`, `export CLAUDE_CODE_AUTO_COMPACT_WINDOW=${context}`);
  }
  lines.push(
    'export CLAUDE_CODE_USE_BEDROCK=0 CLAUDE_CODE_USE_VERTEX=0 CLAUDE_CODE_USE_FOUNDRY=0',
    'unset ANTHROPIC_API_KEY',
    `claude --disallowedTools WebSearch --model ${model} --permission-mode default`,
  );
  return lines.join('\n');
}

// ---------- latency stages (server/latency.py STAGES) ----------

export const STAGES = [
  'http_request',
  'upload',
  'preparation_queue',
  'preparation',
  'template',
  'tokenization',
  'grammar',
  'images',
  'native_queue',
  'http_ttft',
  'output_interval',
] as const;

export type Stage = (typeof STAGES)[number];

/** Mean in ms from a raw `/status.latency.<stage>` histogram (`sum` is seconds). */
export function stageMeanMs(raw: unknown): number | null {
  if (!isRecord(raw)) return null;
  const count = typeof raw.count === 'number' ? raw.count : 0;
  const sum = typeof raw.sum === 'number' ? raw.sum : null;
  return count > 0 && sum !== null ? (sum / count) * 1000 : null;
}

/** Bucket rows `[bound, cumulative]` from a raw histogram, in bound order. */
export function stageBuckets(raw: unknown): Array<[string, number]> {
  if (!isRecord(raw) || !isRecord(raw.buckets)) return [];
  return Object.entries(raw.buckets)
    .filter((e): e is [string, number] => typeof e[1] === 'number')
    .sort((a, b) => (a[0] === '+Inf' ? 1 : b[0] === '+Inf' ? -1 : Number(a[0]) - Number(b[0])));
}

/**
 * A percentile in ms estimated from a cumulative histogram: the upper bound
 * (seconds) of the first bucket whose cumulative count reaches q·count. The `+Inf` bucket
 * reports the largest finite bound. null without samples.
 */
export function stagePercentileMs(raw: unknown, q: number): number | null {
  if (!isRecord(raw)) return null;
  const count = typeof raw.count === 'number' ? raw.count : 0;
  if (count <= 0) return null;
  const rows = stageBuckets(raw);
  const target = q * count;
  let lastFinite: number | null = null;
  for (const [bound, cum] of rows) {
    const b = bound === '+Inf' ? null : Number(bound);
    if (b !== null && Number.isFinite(b)) lastFinite = b;
    if (cum >= target) return (b ?? lastFinite ?? 0) * 1000;
  }
  return lastFinite === null ? null : lastFinite * 1000;
}

// ---------- raw /status access ----------

/** Reads a dotted path from the raw /status document; non-numbers read as null. */
export function rawNum(doc: unknown, path: string): number | null {
  let cur: unknown = doc;
  for (const part of path.split('.')) {
    if (!isRecord(cur)) return null;
    cur = cur[part];
  }
  return typeof cur === 'number' && Number.isFinite(cur) ? cur : null;
}

export function rawGet(doc: unknown, path: string): unknown {
  let cur: unknown = doc;
  for (const part of path.split('.')) {
    if (!isRecord(cur)) return undefined;
    cur = cur[part];
  }
  return cur;
}

/** `state.checkpoint_*` keys rendered as `name value` (the set is version-dependent). */
export function checkpointEntries(state: unknown): Array<[string, number]> {
  if (!isRecord(state)) return [];
  return Object.entries(state)
    .filter((e): e is [string, number] => e[0].startsWith('checkpoint_') && typeof e[1] === 'number')
    .map(([k, v]) => [k.slice('checkpoint_'.length), v]);
}

/** Failure counters for the Cache band; any > 0 is accent. */
export const FAILURE_FIELDS: ReadonlyArray<[string, string]> = [
  ['copies', 'disk.kv_copy_failures'],
  ['restores', 'disk.kv_restore_failures'],
  ['offloads', 'state.offload_failures'],
  ['demotions', 'disk.kv_demotion_failures'],
  ['replay', 'cache.replay_state_publication_failures'],
];
