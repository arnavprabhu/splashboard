/**
 * Global signals: engine state, alerts, auth, settings cache. Theme lives in ./theme.
 * Pages read these directly; only this module writes them.
 */

import { computed, signal } from '@preact/signals';
import { useEffect, useRef } from 'preact/hooks';
import type { DownloadItem } from '../api/models';
import { ADMIN, api, request, setUnauthorizedHandler } from '../api/client';
import { subscribe, type Subscription } from '../api/sse';
import {
  readAlert,
  readAlertList,
  readAuthState,
  readEngineSummary,
  readSettingsResponse,
  type Alert,
  type AlertAction,
  type AlertSeverity,
  type AuthState,
  type EngineState,
  type EngineSummary,
  type SettingsResponse,
} from '../api/types';
import { adoptThemeDefault } from './theme';

export * from './theme';

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

// ---------- engine ----------

export const engine = signal<EngineSummary | null>(null);
export const engineState = computed<EngineState | null>(() => engine.value?.state ?? null);
/** null until the first answer; false once the manager is unreachable. */
export const managerReachable = signal<boolean | null>(null);

export function setEngine(value: unknown): void {
  const summary = readEngineSummary(value);
  if (summary) engine.value = summary;
}

export async function refreshEngine(): Promise<void> {
  try {
    setEngine(await api.get<unknown>('/engine'));
    managerReachable.value = true;
  } catch (err) {
    if (isNetworkError(err)) managerReachable.value = false;
  }
}

function isNetworkError(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { status?: unknown }).status === 0;
}

// ---------- alerts ----------

const SEVERITY_RANK: Record<AlertSeverity, number> = { critical: 0, warn: 1, info: 2 };

export const alerts = signal<Alert[]>([]);

function raisedAt(a: Alert): number {
  const t = a.raised_at ? Date.parse(a.raised_at) : NaN;
  return Number.isFinite(t) ? t : -Infinity;
}

/** Most severe first (SPEC §10.1); newest first within a severity; otherwise stable. */
export function sortAlerts(list: readonly Alert[]): Alert[] {
  return [...list].sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] || raisedAt(b) - raisedAt(a));
}

export const sortedAlerts = computed(() => sortAlerts(alerts.value));

export function upsertAlert(alert: Alert): void {
  const rest = alerts.value.filter((a) => a.id !== alert.id);
  alerts.value = [...rest, alert];
}

export function clearAlert(id: string): void {
  alerts.value = alerts.value.filter((a) => a.id !== id);
}

export function canDismiss(alert: Alert): boolean {
  return alert.severity !== 'critical' && alert.dismissible !== false;
}

export async function refreshAlerts(): Promise<void> {
  try {
    const list = readAlertList(await api.get<unknown>('/alerts'));
    if (list) alerts.value = list;
  } catch {
    /* the event stream delivers them too */
  }
}

/** Hides the alert now and tells the manager (POST /alerts/{id}/dismiss), best effort. */
export async function dismissAlert(alert: Alert): Promise<void> {
  clearAlert(alert.id);
  try {
    await api.post(`/alerts/${encodeURIComponent(alert.id)}/dismiss`);
  } catch {
    /* already hidden locally */
  }
}

export type ActionTarget = { kind: 'navigate'; to: string } | { kind: 'request'; url: string };

/**
 * Where a NotificationAction goes. API paths (`/api/...`, `/v1/...`) are called as
 * given; other GETs are admin pages (`/admin/logs` or `/logs`); other methods are
 * admin API routes relative to /api/admin (`/engine/restart`).
 */
export function actionTarget(action: AlertAction): ActionTarget {
  const path = action.path.startsWith('/') ? action.path : `/${action.path}`;
  if (path.startsWith('/api/') || path.startsWith('/v1/')) return { kind: 'request', url: path };
  if (action.method === 'GET') return { kind: 'navigate', to: path.replace(/^\/admin(?=\/|$)/, '') || '/' };
  return { kind: 'request', url: `${ADMIN}${path}` };
}

export async function runAlertAction(action: AlertAction, navigate: (to: string) => void): Promise<void> {
  const target = actionTarget(action);
  if (target.kind === 'navigate') {
    navigate(target.to);
    return;
  }
  await request(target.url, { method: action.method, body: action.body ?? (action.method === 'GET' ? undefined : {}) });
}

// ---------- auth ----------

export const auth = signal<AuthState>({ admin_requires_key: false, authenticated: true, method: null });
/** True once GET /auth/state has answered. */
export const authKnown = signal(false);

export async function refreshAuth(): Promise<void> {
  try {
    const state = readAuthState(await api.get<unknown>('/auth/state'));
    if (state) {
      auth.value = state;
      authKnown.value = true;
    }
  } catch {
    /* keep the last known state */
  }
}

export function setAuth(value: unknown): void {
  const state = readAuthState(value);
  if (state) {
    auth.value = state;
    authKnown.value = true;
  }
}

export async function logout(): Promise<void> {
  try {
    await api.post('/auth/logout');
  } finally {
    auth.value = { ...auth.value, authenticated: false, method: null };
    // With sign-in off, reads stay open after logout; let the manager say which it is.
    await refreshAuth();
  }
}

/** After a login or a one-time link exchange: take the new state, refetch, reconnect events. */
export async function signedIn(state: unknown): Promise<void> {
  if (readAuthState(state)) setAuth(state);
  else await refreshAuth();
  refreshAll();
  connectEvents();
}

/** `POST /auth/exchange {code}` (D58): trades a one-time link code for the session cookie. */
export async function exchangeLoginCode(code: string): Promise<void> {
  await signedIn(await api.post<unknown>('/auth/exchange', { code }));
}

/**
 * A post-login target from `?next=`: only same-app paths, never another origin. The fallback is
 * the admin root, which sends a fresh install to the wizard and everyone else to Status.
 */
export function safeNext(next: string | null | undefined, fallback = '/'): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return fallback;
  const path = next.replace(/^\/admin(?=\/|$)/, '') || '/';
  return path === '/login' ? fallback : path;
}

// ---------- settings cache ----------

export const settings = signal<SettingsResponse | null>(null);
/** True once a settings load has finished, whether or not it succeeded. */
export const settingsLoaded = signal(false);

let settingsRequest: Promise<SettingsResponse | null> | null = null;

export function loadSettings(force = false): Promise<SettingsResponse | null> {
  if (settings.value && !force) return Promise.resolve(settings.value);
  settingsRequest ??= api
    .get<unknown>('/settings')
    .then((body) => {
      settings.value = readSettingsResponse(body);
      const ui = settings.value?.settings.global.ui;
      adoptThemeDefault(ui?.theme);
      return settings.value;
    })
    .catch(() => settings.value)
    .finally(() => {
      settingsRequest = null;
      settingsLoaded.value = true;
    });
  return settingsRequest;
}

/** `wizard.completed` from settings.json; null while unknown. */
export const wizardCompleted = computed<boolean | null>(() => {
  const wizard = settings.value?.settings.global.wizard;
  return wizard && typeof wizard.completed === 'boolean' ? wizard.completed : null;
});

// ---------- downloads (SPEC §9.4; nav "MODELS 42%", page titles) ----------

export const downloads = signal<DownloadItem[]>([]);

const ACTIVE_DOWNLOAD: ReadonlySet<string> = new Set(['queued', 'running', 'verifying', 'paused']);

/** Queued, running, verifying or paused: the download still owns its model (not done, failed or cancelled). */
export const isActiveDownload = (d: Pick<DownloadItem, 'state'>): boolean => ACTIVE_DOWNLOAD.has(d.state);

export function upsertDownload(item: DownloadItem): void {
  const rest = downloads.value.filter((d) => d.id !== item.id);
  downloads.value = [...rest, item].sort((a, b) => a.created_at.localeCompare(b.created_at));
}

/** Overall progress (0–1) of running downloads, or null when none run. */
export const downloadProgress = computed<number | null>(() => {
  const running = downloads.value.filter((d) => d.state === 'running' || d.state === 'verifying');
  if (running.length === 0) return null;
  const total = running.reduce((sum, d) => sum + (d.bytes_total ?? 0), 0);
  if (total > 0) return running.reduce((sum, d) => sum + d.bytes_done, 0) / total;
  const ps = running.map((d) => d.progress ?? 0);
  return ps.reduce((a, b) => a + b, 0) / ps.length;
});

export const activeDownloads = computed(() => downloads.value.filter(isActiveDownload));

function readDownload(v: unknown): DownloadItem | null {
  return isRecord(v) && typeof v.id === 'string' && typeof v.model === 'string' && typeof v.state === 'string'
    ? ({ files: [], log_tail: [], bytes_done: 0, created_at: '', ...v } as unknown as DownloadItem)
    : null;
}

export async function refreshDownloads(): Promise<void> {
  try {
    const body = await api.get<{ items?: unknown[] }>('/downloads');
    if (Array.isArray(body?.items)) downloads.value = body.items.map(readDownload).filter((d): d is DownloadItem => d !== null);
  } catch {
    /* the event stream delivers them too */
  }
}

// ---------- event bus ----------

/**
 * Event names on GET /api/admin/events (docs/api.md §4). The older names (`engine`,
 * `alerts`, `alert_cleared`, `settings`, `auth`) are still accepted.
 */
export const EVENT_NAMES = [
  'hello',
  'engine.state',
  'alert',
  'alert.cleared',
  'notification',
  'download.progress',
  'download.state',
  'models.changed',
  'settings.changed',
  'integration.state',
  'engine.upgrade',
  'job',
  'benchmark.progress',
  'usage.request',
  // legacy names
  'engine',
  'alerts',
  'alert_cleared',
  'settings',
  'auth',
] as const;

export type EventName = (typeof EVENT_NAMES)[number];
type Listener = (data: unknown) => void;
const listeners = new Map<string, Set<Listener>>();

/** Subscribes to one event on the shared stream; returns the unsubscribe function. */
export function onEvent(name: EventName, fn: Listener): () => void {
  let set = listeners.get(name);
  if (!set) listeners.set(name, (set = new Set()));
  set.add(fn);
  return () => set.delete(fn);
}

/** Hook form of onEvent; the latest handler is always used. */
export function useEvent(name: EventName, fn: Listener): void {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => onEvent(name, (d) => ref.current(d)), [name]);
}

function emit(name: string, data: unknown): void {
  for (const fn of listeners.get(name) ?? []) {
    try {
      fn(data);
    } catch (err) {
      console.error(`[events] ${name} listener failed`, err);
    }
  }
}

export function handleEvent(event: string, data: unknown): void {
  switch (event) {
    case 'hello': {
      if (isRecord(data)) {
        setEngine(data.engine);
        const list = readAlertList(data.alerts ?? []);
        if (list) alerts.value = list;
        if (Array.isArray(data.downloads)) downloads.value = data.downloads.map(readDownload).filter((d): d is DownloadItem => d !== null);
      }
      managerReachable.value = true;
      break;
    }
    case 'engine':
    case 'engine.state':
      setEngine(data);
      managerReachable.value = true;
      break;
    case 'alert': {
      const alert = readAlert(data);
      if (alert) upsertAlert(alert);
      break;
    }
    case 'alert_cleared':
    case 'alert.cleared':
      if (typeof data === 'string') clearAlert(data);
      else if (data && typeof (data as { id?: unknown }).id === 'string') clearAlert((data as { id: string }).id);
      break;
    case 'alerts': {
      const list = readAlertList(data);
      if (list) alerts.value = list;
      break;
    }
    case 'download.progress':
    case 'download.state': {
      const item = readDownload(data);
      if (item) upsertDownload(item);
      break;
    }
    case 'settings':
    case 'settings.changed':
      void loadSettings(true);
      break;
    case 'auth':
      setAuth(data);
      break;
    default:
      break;
  }
  emit(event, data);
}

let events: Subscription | null = null;
/** Attempt counter for the offline screen's countdown. */
export const eventsAttempt = signal(0);
export const lastSeen = signal<string | null>(null);

/** True while the event stream is connected; when it is not, the shell polls (below). */
export const eventsConnected = signal(false);
let pollTimer: ReturnType<typeof setInterval> | null = null;
export const POLL_MS = 3000;

function startPolling(): void {
  if (pollTimer !== null) return;
  pollTimer = setInterval(() => {
    void refreshEngine();
    void refreshAlerts();
    void refreshDownloads();
  }, POLL_MS);
}

function stopPolling(): void {
  if (pollTimer !== null) clearInterval(pollTimer);
  pollTimer = null;
}

/**
 * The manager is reachable when any admin request answers, even if the event stream fails
 * (e.g. a 501 while the backend is incomplete, or a proxy that drops SSE). Only a network
 * failure of the probe marks it offline (docs/ui/01 §5.1).
 */
async function probeManager(): Promise<void> {
  try {
    await request('/health', { method: 'GET' });
    if (managerReachable.value !== true) managerReachable.value = true;
    void refreshEngine();
    startPolling();
  } catch {
    // /health only fails when the manager (or the dev proxy's target) is down.
    managerReachable.value = false;
  }
}

export function connectEvents(): () => void {
  events?.close();
  events = subscribe('/api/admin/events', {
    events: EVENT_NAMES,
    onOpen: () => {
      eventsAttempt.value = 0;
      eventsConnected.value = true;
      stopPolling();
      managerReachable.value = true;
      void refreshEngine();
      void refreshAlerts();
    },
    onError: (attempt) => {
      eventsAttempt.value = attempt;
      eventsConnected.value = false;
      if (managerReachable.value) lastSeen.value = new Date().toLocaleTimeString('en-GB');
      void probeManager();
    },
    onMessage: ({ event, data }) => {
      if (event === 'message' && data && typeof data === 'object' && 'type' in data) {
        const { type, ...rest } = data as { type: string; data?: unknown };
        handleEvent(type, 'data' in rest ? rest.data : rest);
      } else {
        handleEvent(event, data);
      }
    },
  });
  return () => {
    events?.close();
    events = null;
    stopPolling();
  };
}

/** Reconnects the event stream now (offline screen "Retry"). */
export function reconnectEvents(): void {
  connectEvents();
}

/** Refetches everything the shell shows, e.g. after signing in. */
export function refreshAll(): void {
  void refreshEngine();
  void refreshAlerts();
  void loadSettings(true);
}

/** Boot-time wiring used by main.tsx. */
export function startStore(onUnauthorized: (expired: boolean) => void): () => void {
  setUnauthorizedHandler(() => {
    // A 401 after a browser session was established means the session expired (docs/ui/01 §6).
    const expired = auth.value.method === 'session';
    auth.value = { ...auth.value, authenticated: false, method: null };
    onUnauthorized(expired);
  });
  void refreshAuth();
  void refreshEngine();
  void refreshAlerts();
  void refreshDownloads();
  void loadSettings();
  return connectEvents();
}
