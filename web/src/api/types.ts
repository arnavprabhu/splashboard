/**
 * Hand-written shapes the shell depends on, until src/api/schema.d.ts is generated
 * from the manager's OpenAPI document. They follow manager/splash_gui/schemas.py
 * (EngineView, Alert, AuthState, SettingsResponse). Readers are tolerant: missing
 * fields fall back to null and a few older field names are still accepted.
 */

/** `EngineView.state` from the manager (SPEC §6.3). */
export type EngineStateName =
  | 'stopped'
  | 'starting'
  | 'ready'
  | 'busy'
  | 'idle_released'
  | 'recovering'
  | 'engine_failed'
  | 'stopping'
  | 'crashed'
  | 'failed';

/** `EngineView.phase`, set while `state` is `starting`. */
export type EnginePhase = 'installing' | 'loading' | 'warming';

/** UI state: `starting` is flattened with its phase, as SPEC §6.3 writes it. */
export type EngineState = Exclude<EngineStateName, 'starting'> | `starting.${EnginePhase}`;

export const ENGINE_STATES: readonly EngineState[] = [
  'stopped',
  'starting.installing',
  'starting.loading',
  'starting.warming',
  'ready',
  'busy',
  'idle_released',
  'recovering',
  'engine_failed',
  'stopping',
  'crashed',
  'failed',
];

const PHASES: readonly EnginePhase[] = ['installing', 'loading', 'warming'];

/** `EngineView.error` (startup or crash error parsed by the supervisor, SPEC §6.4). */
export interface EngineErrorInfo {
  kind: string;
  code: string;
  message: string;
  raw: string[];
}

/** `GET /api/admin/engine` (SPEC §14), reduced to what the shell shows. */
export interface EngineSummary {
  state: EngineState;
  phase: EnginePhase | null;
  model: string | null;
  /** `EngineView.engine.version`: the discovered Splash version. */
  engine_version: string | null;
  uptime_s: number | null;
  requests_in_flight: number;
  queued: number;
  maximum_context_tokens: number | null;
  kv_format: string | null;
  vision: boolean | null;
  draft: string | null;
  error: EngineErrorInfo | null;
  /** The full EngineView as received, for pages that need more. */
  view: Record<string, unknown>;
}

export type AlertSeverity = 'critical' | 'warn' | 'info';

/** `NotificationAction`: an API call (or, for GET on a non-API path, an admin page). */
export interface AlertAction {
  id: string;
  label: string;
  method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  path: string;
  body?: Record<string, unknown>;
}

/** Health alerts (SPEC §16.3), from `GET /api/admin/alerts` and the event stream. */
export interface Alert {
  id: string;
  severity: AlertSeverity;
  condition?: string;
  title?: string;
  message: string;
  actions: AlertAction[];
  raised_at?: string;
  updated_at?: string;
  count?: number;
  dismissible?: boolean;
}

/** `GET /api/admin/auth/state`. */
export interface AuthState {
  admin_requires_key: boolean;
  authenticated: boolean;
  method: 'session' | 'cli_token' | 'open' | null;
}

/** The settings.json document (SPEC §15.1). */
export interface SettingsDoc {
  version: number;
  global: Record<string, Record<string, unknown>>;
  models: Record<string, Record<string, unknown>>;
}

/** `GET /api/admin/settings`. */
export interface SettingsResponse {
  settings: SettingsDoc;
  /** The folders Splash actually uses (SPEC §8.2); the defaults unless storage moves them. */
  resolved?: { home?: string; cache_dir?: string };
  read_only?: boolean;
  load_warnings?: string[];
  [extra: string]: unknown;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (x: unknown): string | null => (typeof x === 'string' && x ? x : null);
const num = (x: unknown): number | null => (typeof x === 'number' && Number.isFinite(x) ? x : null);

export function isEngineState(v: unknown): v is EngineState {
  return typeof v === 'string' && (ENGINE_STATES as readonly string[]).includes(v);
}

function readPhase(v: unknown): EnginePhase | null {
  return PHASES.includes(v as EnginePhase) ? (v as EnginePhase) : null;
}

/** Maps `{state: "starting", phase: "warming"}` to `starting.warming`; unknown states read as stopped. */
export function flattenState(state: unknown, phase: unknown): EngineState {
  if (state === 'starting') return `starting.${readPhase(phase) ?? 'loading'}`;
  return isEngineState(state) ? state : 'stopped';
}

function readEngineError(v: unknown): EngineErrorInfo | null {
  if (typeof v === 'string' && v) return { kind: 'other', code: '', message: v, raw: [] };
  if (!isRecord(v) || typeof v.message !== 'string') return null;
  return {
    kind: str(v.kind) ?? 'other',
    code: str(v.code) ?? '',
    message: v.message,
    raw: Array.isArray(v.raw) ? v.raw.filter((l): l is string => typeof l === 'string') : [],
  };
}

export function readEngineSummary(v: unknown): EngineSummary | null {
  if (!isRecord(v)) return null;
  const state = flattenState(v.state, v.phase);
  const discovery = isRecord(v.engine) ? v.engine : {};
  return {
    state,
    phase: state.startsWith('starting.') ? (state.slice('starting.'.length) as EnginePhase) : null,
    model: str(v.model),
    engine_version: str(discovery.version) ?? str(v.engine_version) ?? str(v.version),
    uptime_s: num(v.uptime_s),
    requests_in_flight: num(v.requests_in_flight) ?? 0,
    queued: num(v.queued) ?? 0,
    maximum_context_tokens: num(v.maximum_context_tokens),
    kv_format: str(v.kv_format),
    vision: typeof v.vision === 'boolean' ? v.vision : null,
    draft: str(v.draft),
    error: readEngineError(v.error),
    view: v,
  };
}

const SEVERITIES: readonly AlertSeverity[] = ['critical', 'warn', 'info'];
const METHODS: readonly AlertAction['method'][] = ['GET', 'POST', 'PUT', 'DELETE'];

function readAction(v: unknown, index: number): AlertAction | null {
  if (!isRecord(v) || typeof v.label !== 'string') return null;
  // Older shape: {label, href} navigates, {label, command} posts.
  const path = str(v.path) ?? str(v.href) ?? str(v.command);
  if (!path) return null;
  const method = METHODS.includes(v.method as AlertAction['method'])
    ? (v.method as AlertAction['method'])
    : str(v.path) || str(v.command)
      ? 'POST'
      : 'GET';
  const action: AlertAction = { id: str(v.id) ?? `action-${index}`, label: v.label, method, path };
  if (isRecord(v.body)) action.body = v.body;
  return action;
}

export function readAlert(v: unknown): Alert | null {
  if (!isRecord(v) || typeof v.message !== 'string') return null;
  const severity = SEVERITIES.includes(v.severity as AlertSeverity) ? (v.severity as AlertSeverity) : 'warn';
  const rawActions = Array.isArray(v.actions) ? v.actions : v.action !== undefined ? [v.action] : [];
  const alert: Alert = {
    id: str(v.id) ?? str(v.condition) ?? v.message,
    severity,
    message: v.message,
    actions: rawActions.map(readAction).filter((a): a is AlertAction => a !== null),
  };
  const condition = str(v.condition);
  if (condition) alert.condition = condition;
  const title = str(v.title);
  if (title) alert.title = title;
  const raised = str(v.raised_at) ?? str(v.since);
  if (raised) alert.raised_at = raised;
  const updated = str(v.updated_at);
  if (updated) alert.updated_at = updated;
  const count = num(v.count);
  if (count !== null) alert.count = count;
  if (typeof v.dismissible === 'boolean') alert.dismissible = v.dismissible;
  return alert;
}

/** Accepts `{alerts: [...]}` (GET /alerts) or a bare list (the `alerts` event). */
export function readAlertList(v: unknown): Alert[] | null {
  const list = Array.isArray(v) ? v : isRecord(v) && Array.isArray(v.alerts) ? v.alerts : null;
  return list ? list.map(readAlert).filter((a): a is Alert => a !== null) : null;
}

export function readAuthState(v: unknown): AuthState | null {
  if (!isRecord(v)) return null;
  const required = typeof v.admin_requires_key === 'boolean' ? v.admin_requires_key : Boolean(v.enabled);
  const method = v.method === 'session' || v.method === 'cli_token' || v.method === 'open' ? v.method : null;
  return { admin_requires_key: required, authenticated: v.authenticated !== false, method };
}

/** Accepts the SettingsResponse envelope or a bare document. */
export function readSettingsResponse(v: unknown): SettingsResponse | null {
  if (!isRecord(v)) return null;
  const doc = isRecord(v.settings) ? v.settings : isRecord(v.global) ? v : null;
  if (!doc) return null;
  return {
    ...(doc === v ? {} : v),
    settings: {
      version: num(doc.version) ?? 1,
      global: (isRecord(doc.global) ? doc.global : {}) as SettingsDoc['global'],
      models: (isRecord(doc.models) ? doc.models : {}) as SettingsDoc['models'],
    },
  };
}
