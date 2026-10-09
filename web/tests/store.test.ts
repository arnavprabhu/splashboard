import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  flattenState,
  readAlert,
  readAlertList,
  readAuthState,
  readEngineSummary,
  readSettingsResponse,
  type Alert,
} from '../src/api/types';
import { stateDisplay } from '../src/lib/engine-state';
import {
  actionTarget,
  alerts,
  auth,
  authKnown,
  canDismiss,
  clearAlert,
  dismissAlert,
  engine,
  handleEvent,
  loadSettings,
  runAlertAction,
  safeNext,
  settings,
  settingsLoaded,
  sortAlerts,
  sortedAlerts,
  upsertAlert,
  wizardCompleted,
} from '../src/store';
import { themePref, THEME_STORAGE_KEY } from '../src/store/theme';

const alert = (id: string, severity: Alert['severity'], extra: Partial<Alert> = {}): Alert => ({
  id,
  severity,
  message: id,
  actions: [],
  ...extra,
});

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** A manager EngineView (manager/splash_gui/schemas.py). */
const VIEW = {
  state: 'starting',
  phase: 'warming',
  model: 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL',
  since: '2026-10-03T12:00:00+00:00',
  uptime_s: 12.5,
  requests_in_flight: 2,
  queued: 1,
  maximum_context_tokens: 131072,
  kv_format: 'q8_0',
  vision: true,
  draft: null,
  restart: { auto_restart: true, attempt: 0, crashes_in_window: 0 },
  error: { kind: 'budget_refusal', code: 'budget_refusal', message: 'Model does not fit', raw: ['line 1'], suggestions: [] },
  engine: { found: true, cli: '/opt/homebrew/bin/splash', source: 'brew', version: '1.2.0', support: 'supported' },
};

beforeEach(() => {
  alerts.value = [];
  engine.value = null;
  settings.value = null;
  settingsLoaded.value = false;
  localStorage.clear();
});

describe('engine summary', () => {
  it('reads the manager EngineView', () => {
    const s = readEngineSummary(VIEW);
    expect(s).toMatchObject({
      state: 'starting.warming',
      phase: 'warming',
      model: VIEW.model,
      engine_version: '1.2.0',
      uptime_s: 12.5,
      requests_in_flight: 2,
      queued: 1,
      maximum_context_tokens: 131072,
      kv_format: 'q8_0',
      vision: true,
      draft: null,
      error: { kind: 'budget_refusal', code: 'budget_refusal', message: 'Model does not fit', raw: ['line 1'] },
    });
    expect(s?.view).toBe(VIEW);
  });

  it('flattens starting phases and tolerates older or unknown shapes', () => {
    expect(flattenState('starting', 'installing')).toBe('starting.installing');
    expect(flattenState('starting', undefined)).toBe('starting.loading');
    expect(flattenState('starting.warming', undefined)).toBe('starting.warming');
    expect(flattenState('bogus', undefined)).toBe('stopped');
    expect(readEngineSummary({ state: 'ready', engine_version: '1.2.0', error: 'boom' })).toMatchObject({
      state: 'ready',
      phase: null,
      engine_version: '1.2.0',
      error: { message: 'boom', kind: 'other' },
    });
    expect(readEngineSummary({ state: 'busy', version: '1.2.1' })?.engine_version).toBe('1.2.1');
    expect(readEngineSummary(null)).toBeNull();
  });

  it('maps states to chips: accent only for live', () => {
    expect(stateDisplay('ready')).toEqual({ label: 'Ready', live: true });
    expect(stateDisplay('busy')).toEqual({ label: 'Generating', live: true });
    expect(stateDisplay('starting.loading').live).toBe(false);
    expect(stateDisplay('idle_released')).toEqual({ label: 'Idle', live: false });
    expect(stateDisplay('engine_failed')).toEqual({ label: 'Failed', live: false });
    expect(stateDisplay(null)).toEqual({ label: 'Offline', live: false });
  });
});

describe('alerts', () => {
  it('sorts most severe first, newest first within a severity, otherwise stable', () => {
    const list = sortAlerts([
      alert('a', 'info'),
      alert('b', 'warn', { raised_at: '2026-10-03T10:00:00Z' }),
      alert('c', 'critical'),
      alert('d', 'warn', { raised_at: '2026-10-03T11:00:00Z' }),
      alert('e', 'info'),
    ]);
    expect(list.map((x) => x.id)).toEqual(['c', 'd', 'b', 'a', 'e']);
  });

  it('upserts by id and clears', () => {
    upsertAlert(alert('x', 'warn', { message: 'one' }));
    upsertAlert(alert('x', 'critical', { message: 'two' }));
    upsertAlert(alert('y', 'info', { message: 'three' }));
    expect(sortedAlerts.value.map((a) => a.message)).toEqual(['two', 'three']);
    clearAlert('x');
    expect(alerts.value.map((a) => a.id)).toEqual(['y']);
  });

  it('reads the manager Alert shape', () => {
    expect(
      readAlert({
        id: 'engine_failed',
        condition: 'engine_failed',
        severity: 'critical',
        title: 'Engine stopped',
        message: 'Engine stopped after repeated failures',
        source: 'status',
        raised_at: '2026-10-03T12:00:00Z',
        updated_at: '2026-10-03T12:01:00Z',
        count: 2,
        dismissible: false,
        actions: [{ id: 'restart', label: 'Restart', method: 'POST', path: '/api/admin/engine/restart' }, { label: 'bad' }],
      }),
    ).toEqual({
      id: 'engine_failed',
      condition: 'engine_failed',
      severity: 'critical',
      title: 'Engine stopped',
      message: 'Engine stopped after repeated failures',
      raised_at: '2026-10-03T12:00:00Z',
      updated_at: '2026-10-03T12:01:00Z',
      count: 2,
      dismissible: false,
      actions: [{ id: 'restart', label: 'Restart', method: 'POST', path: '/api/admin/engine/restart' }],
    });
  });

  it('reads older alert shapes defensively', () => {
    expect(readAlert({ message: 'm', severity: 'nope' })).toEqual({ id: 'm', severity: 'warn', message: 'm', actions: [] });
    expect(readAlert({ id: 'i', message: 'm', severity: 'warn', since: 't', action: { label: 'Logs', href: '/logs' } })).toEqual({
      id: 'i',
      severity: 'warn',
      message: 'm',
      raised_at: 't',
      actions: [{ id: 'action-0', label: 'Logs', method: 'GET', path: '/logs' }],
    });
    expect(readAlert({ id: 'i', message: 'm', action: { label: 'Restart', command: '/engine/restart' } })?.actions[0]?.method).toBe(
      'POST',
    );
    expect(readAlert({ severity: 'warn' })).toBeNull();
    expect(readAlertList({ alerts: [{ id: 'a', message: 'a' }, 7] })?.map((a) => a.id)).toEqual(['a']);
    expect(readAlertList([{ id: 'b', message: 'b' }])?.map((a) => a.id)).toEqual(['b']);
    expect(readAlertList({ nope: 1 })).toBeNull();
  });

  it('never offers Dismiss on critical alerts or when the manager forbids it', () => {
    expect(canDismiss(alert('a', 'warn'))).toBe(true);
    expect(canDismiss(alert('a', 'critical'))).toBe(false);
    expect(canDismiss(alert('a', 'info', { dismissible: false }))).toBe(false);
  });

  it('dismisses locally and tells the manager', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(200, { ok: true }));
    upsertAlert(alert('queue_full', 'warn'));
    await dismissAlert(alerts.value[0]!);
    expect(alerts.value).toEqual([]);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/admin/alerts/queue_full/dismiss');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST');
  });
});

describe('alert actions', () => {
  it('routes API paths to requests and other GETs to admin pages', () => {
    expect(actionTarget({ id: 'a', label: 'x', method: 'POST', path: '/api/admin/engine/restart' })).toEqual({
      kind: 'request',
      url: '/api/admin/engine/restart',
    });
    expect(actionTarget({ id: 'a', label: 'x', method: 'POST', path: '/engine/restart' })).toEqual({
      kind: 'request',
      url: '/api/admin/engine/restart',
    });
    expect(actionTarget({ id: 'a', label: 'x', method: 'GET', path: '/admin/logs?source=engine' })).toEqual({
      kind: 'navigate',
      to: '/logs?source=engine',
    });
    expect(actionTarget({ id: 'a', label: 'x', method: 'GET', path: '/settings/cache' })).toEqual({
      kind: 'navigate',
      to: '/settings/cache',
    });
  });

  it('runs requests with the action method and body', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(202, { state: 'starting' }));
    const navigate = vi.fn();
    await runAlertAction(
      { id: 'load', label: 'Load now', method: 'POST', path: '/api/admin/engine/load', body: { model: 'a/b' } },
      navigate,
    );
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/admin/engine/load');
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe('{"model":"a/b"}');
    await runAlertAction({ id: 'logs', label: 'Logs', method: 'GET', path: '/admin/logs' }, navigate);
    expect(navigate).toHaveBeenCalledWith('/logs');
  });
});

describe('auth', () => {
  it('reads the manager AuthState and the older shape', () => {
    expect(readAuthState({ admin_requires_key: true, authenticated: false, method: null })).toEqual({
      admin_requires_key: true,
      authenticated: false,
      method: null,
    });
    expect(readAuthState({ admin_requires_key: false, authenticated: true, method: 'open' })?.method).toBe('open');
    expect(readAuthState({ enabled: true, authenticated: true })?.admin_requires_key).toBe(true);
    expect(readAuthState('x')).toBeNull();
  });

  it('handles auth events', () => {
    authKnown.value = false;
    handleEvent('auth', { admin_requires_key: true, authenticated: true, method: 'session' });
    expect(auth.value.method).toBe('session');
    expect(authKnown.value).toBe(true);
  });

  it('only accepts same-app next paths', () => {
    expect(safeNext('/models?model=a%2Fb')).toBe('/models?model=a%2Fb');
    expect(safeNext('/admin/settings/cache')).toBe('/settings/cache');
    expect(safeNext('//evil.example/')).toBe('/');
    expect(safeNext('/\\evil.example')).toBe('/');
    expect(safeNext('https://evil.example/')).toBe('/');
    expect(safeNext('/login')).toBe('/');
    expect(safeNext(null)).toBe('/');
  });
});

describe('settings cache', () => {
  it('unwraps the SettingsResponse envelope or a bare document', () => {
    const doc = { version: 1, global: { ui: { theme: 'dark' } }, models: {} };
    expect(readSettingsResponse({ settings: doc, read_only: false })?.settings).toEqual(doc);
    expect(readSettingsResponse(doc)?.settings).toEqual(doc);
    expect(readSettingsResponse({ nope: true })).toBeNull();
  });

  it('loads once, exposes wizard.completed and adopts ui.theme without overriding a local choice', async () => {
    themePref.value = 'light';
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(json(200, { settings: { version: 1, global: { ui: { theme: 'dark' }, wizard: { completed: false } }, models: {} } }));
    await Promise.all([loadSettings(), loadSettings()]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(settingsLoaded.value).toBe(true);
    expect(wizardCompleted.value).toBe(false);
    expect(themePref.value).toBe('dark');

    themePref.value = 'light';
    localStorage.setItem(THEME_STORAGE_KEY, 'light');
    await loadSettings(true);
    expect(themePref.value).toBe('light');
  });

  it('marks settings as loaded even when the request fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('offline'));
    await loadSettings();
    expect(settingsLoaded.value).toBe(true);
    expect(settings.value).toBeNull();
    expect(wizardCompleted.value).toBeNull();
  });
});

describe('handleEvent', () => {
  it('routes events into signals', () => {
    handleEvent('engine', { state: 'busy', model: 'a/b' });
    expect(engine.value?.state).toBe('busy');
    handleEvent('alert', { id: 'q', severity: 'warn', message: 'Queue full' });
    handleEvent('alert', { id: 'm', severity: 'critical', message: 'Memory' });
    expect(sortedAlerts.value.map((a) => a.id)).toEqual(['m', 'q']);
    handleEvent('alert_cleared', { id: 'm' });
    handleEvent('alert_cleared', 'q');
    expect(alerts.value).toEqual([]);
    handleEvent('alerts', [{ id: 'z', severity: 'info', message: 'z' }, { nope: true }]);
    expect(alerts.value.map((a) => a.id)).toEqual(['z']);
    handleEvent('alerts', { alerts: [{ id: 'w', severity: 'warn', message: 'w' }] });
    expect(alerts.value.map((a) => a.id)).toEqual(['w']);
    settings.value = { settings: { version: 1, global: {}, models: {} } };
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(200, { settings: { version: 1, global: { ui: {} }, models: {} } }));
    handleEvent('settings.changed', { changed: [], restart_required: false });
    expect(fetchSpy).toHaveBeenCalledWith('/api/admin/settings', expect.anything());
    handleEvent('unknown', {});
  });
});
