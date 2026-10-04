/**
 * Global signals: engine state, alerts, auth, settings cache. Theme lives in ./theme.
 * Pages read these directly; only this module writes them.
 */

import { computed, signal } from '@preact/signals';
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
  }
}

/** A post-login target from `?next=`: only same-app paths, never another origin. */
export function safeNext(next: string | null | undefined, fallback = '/status'): string {
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

// ---------- event stream ----------

/** Event names on GET /api/admin/events that the shell consumes. */
export const EVENT_NAMES = ['engine', 'alert', 'alert_cleared', 'alerts', 'settings', 'auth'] as const;

export function handleEvent(event: string, data: unknown): void {
  switch (event) {
    case 'engine':
      setEngine(data);
      managerReachable.value = true;
      break;
    case 'alert': {
      const alert = readAlert(data);
      if (alert) upsertAlert(alert);
      break;
    }
    case 'alert_cleared':
      if (typeof data === 'string') clearAlert(data);
      else if (data && typeof (data as { id?: unknown }).id === 'string') clearAlert((data as { id: string }).id);
      break;
    case 'alerts': {
      const list = readAlertList(data);
      if (list) alerts.value = list;
      break;
    }
    case 'settings':
      settings.value = null;
      break;
    case 'auth':
      setAuth(data);
      break;
    default:
      break;
  }
}

let events: Subscription | null = null;

export function connectEvents(): () => void {
  events?.close();
  events = subscribe('/api/admin/events', {
    events: EVENT_NAMES,
    onOpen: () => {
      managerReachable.value = true;
      void refreshEngine();
      void refreshAlerts();
    },
    onError: (attempt) => {
      if (attempt > 1) managerReachable.value = false;
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
  };
}

/** Refetches everything the shell shows, e.g. after signing in. */
export function refreshAll(): void {
  void refreshEngine();
  void refreshAlerts();
  void loadSettings(true);
}

/** Boot-time wiring used by main.tsx. */
export function startStore(onUnauthorized: () => void): () => void {
  setUnauthorizedHandler(() => {
    auth.value = { ...auth.value, authenticated: false, method: null };
    onUnauthorized();
  });
  void refreshAuth();
  void refreshEngine();
  void refreshAlerts();
  void loadSettings();
  return connectEvents();
}
