/**
 * Client-side validation glue (docs/ui/05 §4, 03 §5.2–5.3). Engine options reuse Splash's own
 * parsers and wording from lib/size.ts; the manager validates again (POST /settings/validate,
 * PUT /settings) and its messages replace these. Messages that are ours live in the string
 * table; Splash's are verbatim here because they are engine text (docs/ui/00 §8.2).
 */

import { parseMaxContext, parseMaxCacheDisk, parseMaxMemory, parseRequestSize } from '../../lib/size';
import { t } from '../../strings/settings';

/** The bits of a schema field the validators need. */
export interface FieldRule {
  key: string;
  control: string;
  min?: number | null;
  max?: number | null;
  label?: string;
}

// Splash's own messages (splash/server/serve_options.py, 1.2.0), shown verbatim.
export const SPLASH_MESSAGES = {
  decodeShare: 'must be a nonnegative number such as 0.5',
  imagePixels: (min: number, max: number) => `must be between ${min} and ${max} pixels`,
  requestTimeout: 'must be a positive number of seconds such as 3600',
  queueSize: 'must be a positive number of requests such as 32',
  alias: 'model alias must be a non-empty name without whitespace or URL delimiters',
  originWildcard: (v: string) =>
    `${v} is not an origin: only a bare '*' admits every origin; origins are matched exactly, so patterns such as tauri://* or http://*.example.com are not supported`,
  originShape: (v: string) =>
    `${v} is not an origin: expected a scheme and a host, as in tauri://localhost or http://localhost:3000, or '*' for every origin`,
  apiKey: 'API key must contain only visible ASCII characters',
} as const;

const isBlank = (v: unknown) => v === null || v === undefined || (typeof v === 'string' && v.trim() === '');

/** `parse_served_model_name` (serve_options.py). */
export function aliasError(value: string): string | null {
  const bad =
    !value ||
    [...value].some((c) => /\s/.test(c) || c < ' ' || c === '\u007f' || '\\%?#'.includes(c)) ||
    value.split('/').some((part) => part === '' || part === '.' || part === '..');
  return bad ? SPLASH_MESSAGES.alias : null;
}

/** A light mirror of Splash's `origins.parse_allowed_origin` (the manager runs the real one). */
export function originError(value: string): string | null {
  const v = value.trim();
  if (v === '*') return null;
  if (v.includes('*')) return SPLASH_MESSAGES.originWildcard(v);
  return /^[A-Za-z][A-Za-z0-9+.-]*:\/\/(\[[0-9A-Fa-f:.]+\]|[^/\s:[\]]+)(:\d{1,5})?$/.test(v) ? null : SPLASH_MESSAGES.originShape(v);
}

/** Allowed hosts are Host names, never URLs. */
export function hostError(value: string): string | null {
  const v = value.trim();
  if (!v || /\s/.test(v) || v.includes('/') || v.includes('://')) return t('settings.validation.host_name');
  return null;
}

/** `validate_api_key`: printable ASCII without spaces. */
export function apiKeyError(value: string): string | null {
  return value.length > 0 && /^[\x21-\x7e]+$/.test(value) ? null : SPLASH_MESSAGES.apiKey;
}

export function urlError(value: string): string | null {
  return /^https?:\/\/[^\s/]+/i.test(value.trim()) ? null : t('settings.validation.url');
}

/** Client-side error for a settings field value, or null. */
export function fieldError(rule: FieldRule, value: unknown): string | null {
  const s = typeof value === 'string' ? value : value === null || value === undefined ? '' : String(value);
  switch (rule.key) {
    case 'serve.max_context':
      return parseMaxContext(s || 'auto').ok ? null : (parseMaxContext(s) as { error: string }).error;
    case 'serve.max_memory': {
      const r = parseMaxMemory(s || 'auto');
      return r.ok ? null : r.error;
    }
    case 'serve.max_cache_disk': {
      const r = parseMaxCacheDisk(s);
      return r.ok ? null : r.error;
    }
    case 'serve.max_request_size': {
      const r = parseRequestSize(s);
      return r.ok ? null : r.error;
    }
    case 'serve.decode_share':
      return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? null : SPLASH_MESSAGES.decodeShare;
    case 'serve.max_image_pixels': {
      const min = rule.min ?? 65536;
      const max = rule.max ?? 4194304;
      return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max ? null : SPLASH_MESSAGES.imagePixels(min, max);
    }
    case 'serve.request_timeout':
      return value === null || value === undefined || (typeof value === 'number' && value > 0) ? null : SPLASH_MESSAGES.requestTimeout;
    case 'serve.queue_size':
      return typeof value === 'number' && Number.isInteger(value) && value >= 1 ? null : SPLASH_MESSAGES.queueSize;
    case 'serve.served_model_names':
      return Array.isArray(value) ? (value.map((v) => aliasError(String(v))).find(Boolean) ?? null) : null;
    case 'server.allowed_origins':
      return Array.isArray(value) ? (value.map((v) => originError(String(v))).find(Boolean) ?? null) : null;
    case 'server.allowed_hosts':
      return Array.isArray(value) ? (value.map((v) => hostError(String(v))).find(Boolean) ?? null) : null;
    case 'server.host':
      return isBlank(value) || /\s/.test(s) ? t('settings.validation.host_ip') : null;
    case 'server.port':
    case 'engine.internal_port':
    case 'integrations.claude_desktop.port': {
      if (value === 'auto' && rule.key === 'engine.internal_port') return null;
      const min = rule.min ?? 1;
      const max = rule.max ?? 65535;
      return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max ? null : t('settings.validation.port', { min, max });
    }
    case 'hf.endpoint':
      return isBlank(value) ? null : urlError(s);
    default:
      break;
  }
  if (rule.control === 'number' || rule.control === 'duration') {
    if (value === null || value === undefined) return rule.control === 'duration' ? null : t('settings.validation.required');
    if (typeof value !== 'number' || !Number.isFinite(value)) return t('settings.validation.number');
    if (rule.min !== null && rule.min !== undefined && rule.max !== null && rule.max !== undefined && (value < rule.min || value > rule.max))
      return t('settings.validation.between', { min: rule.min, max: rule.max });
    if (rule.min !== null && rule.min !== undefined && value < rule.min) return t('settings.validation.at_least', { min: rule.min });
    if (rule.max !== null && rule.max !== undefined && value > rule.max) return t('settings.validation.at_most', { max: rule.max });
  }
  if (rule.control === 'url' && !isBlank(value)) return urlError(s);
  return null;
}

// ---------- sampling overlays and profiles (docs/api.md §6.1, docs/ui/03 §5.2) ----------

/** `[a-z0-9][a-z0-9_-]{0,31}` (docs/api.md §1.1; supersedes 03 M8's hyphen-only rule). */
export const PROFILE_NAME = /^[a-z0-9][a-z0-9_-]{0,31}$/;

export function profileNameError(name: string, existing: readonly string[], original?: string | null): string | null {
  if (!PROFILE_NAME.test(name)) return t('settings.profiles.name_rule');
  if (name !== original && existing.includes(name)) return t('settings.profiles.name_taken', { name });
  return null;
}

/** Error for one overlay field value (Splash's ranges, server/frontend.py). */
export function overlayFieldError(key: string, label: string, value: unknown): string | null {
  if (value === undefined) return null;
  const num = typeof value === 'number' && Number.isFinite(value) ? value : null;
  const between = (min: number, max: number) => (num !== null && num >= min && num <= max ? null : t('settings.overlay.between', { label, min, max }));
  switch (key) {
    case 'temperature':
      return between(0, 2);
    case 'top_p':
      return num !== null && num > 0 && num <= 1 ? null : t('settings.overlay.top_p', { label });
    case 'top_k':
      return num !== null && Number.isInteger(num) && (num === 0 || num === -1 || num >= 1) ? null : t('settings.overlay.top_k');
    case 'min_p':
      return between(0, 1);
    case 'presence_penalty':
    case 'frequency_penalty':
      return between(-2, 2);
    case 'repetition_penalty':
    case 'timeout':
      return num !== null && num > 0 ? null : t('settings.overlay.positive', { label });
    case 'max_tokens':
      return num !== null && Number.isInteger(num) && num >= 1 ? null : t('settings.overlay.whole_min', { label, min: 1 });
    case 'seed':
      return num !== null && Number.isInteger(num) && num >= 0 ? null : t('settings.overlay.whole_min', { label, min: 0 });
    case 'stop': {
      const list = Array.isArray(value) ? value : typeof value === 'string' ? [value] : null;
      if (!list || list.some((s) => typeof s !== 'string' || s === '')) return t('settings.overlay.stop_empty');
      return list.length <= 4 ? null : t('settings.overlay.stop_max');
    }
    case 'chat_template_kwargs':
    case 'thinking':
      return value !== null && typeof value === 'object' && !Array.isArray(value) ? null : t('settings.overlay.json_object', { label });
    default:
      return null;
  }
}

/** Parses a JSON-object text field; returns the object or an error message. */
export function parseJsonObject(text: string, label: string): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  try {
    const value: unknown = JSON.parse(text);
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) return { ok: true, value: value as Record<string, unknown> };
  } catch {
    /* fall through */
  }
  return { ok: false, error: t('settings.overlay.json_object', { label }) };
}

/** One-line summary of an overlay for the profiles table: `temperature 0 · seed 0`. */
export function overlaySummary(overlay: Record<string, unknown>): string {
  const parts = Object.entries(overlay)
    .filter(([, v]) => v !== undefined && v !== null)
    .map(([k, v]) => `${k} ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`);
  return parts.join(' · ');
}

// ---------- raw engine options (docs/ui/05 §7) ----------

export interface RawFlag {
  flag: string;
  value: string | null;
}

/**
 * Parses free text such as `--new-thing 42 --other-switch` into rows. A token that starts with
 * `--` opens a row; the next token that does not is its value. `--flag=value` is split.
 */
export function parseRawFlags(text: string): { ok: true; flags: RawFlag[] } | { ok: false; error: string } {
  const tokens = text.match(/"[^"]*"|'[^']*'|\S+/g) ?? [];
  const flags: RawFlag[] = [];
  for (const raw of tokens) {
    const token = /^(["']).*\1$/.test(raw) ? raw.slice(1, -1) : raw;
    if (token.startsWith('--')) {
      const eq = token.indexOf('=');
      if (eq > 2) flags.push({ flag: token.slice(0, eq), value: token.slice(eq + 1) });
      else if (token.length > 2) flags.push({ flag: token, value: null });
      else return { ok: false, error: t('settings.flags.bad_flag', { flag: token }) };
    } else {
      const last = flags[flags.length - 1];
      if (!last || last.value !== null) return { ok: false, error: t('settings.flags.value_without_flag', { value: token }) };
      last.value = token;
    }
  }
  if (flags.length === 0) return { ok: false, error: t('settings.flags.empty') };
  return { ok: true, flags };
}

/** A flag name Splash would accept as an option string. */
export function flagNameError(flag: string): string | null {
  return /^--[a-z0-9][a-z0-9-]*$/.test(flag) ? null : t('settings.flags.bad_flag', { flag });
}

/** `engine.extra_flags` rows → the argv tail the command preview shows. */
export function flagsToArgv(flags: readonly RawFlag[]): string[] {
  return flags.flatMap((f) => (f.value === null || f.value === undefined ? [f.flag] : [f.flag, f.value]));
}
