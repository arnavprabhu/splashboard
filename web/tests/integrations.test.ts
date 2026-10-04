import { describe, expect, it } from 'vitest';
import {
  SDK_TABS,
  clock,
  desktopView,
  entryFile,
  isLoopbackHost,
  launchCommand,
  normaliseSlots,
  parseVersion,
  removableEntries,
  sdkSnippet,
  slotsSwitchModels,
  snippetOrigin,
  tauriOriginState,
  withTauriOrigin,
} from '../src/routes/integrations/logic';

const MODEL = 'mlx-community/Qwen3.6-35B-A3B-4bit';

describe('launch commands', () => {
  it('appends --model only when the pick differs from the active model (D-09-1)', () => {
    const cli = { command: 'splash launch claude' };
    expect(launchCommand(cli, MODEL, MODEL)).toBe('splash launch claude');
    expect(launchCommand(cli, '', MODEL)).toBe('splash launch claude');
    expect(launchCommand(cli, `${MODEL}:no-think`, MODEL)).toBe(`splash launch claude --model ${MODEL}:no-think`);
    expect(launchCommand(cli, MODEL, null)).toBe(`splash launch claude --model ${MODEL}`);
  });
});

describe('mac-only actions', () => {
  it('treats loopback names as the manager’s Mac', () => {
    for (const h of ['localhost', '127.0.0.1', '127.0.0.2', '[::1]', '::1']) expect(isLoopbackHost(h), h).toBe(true);
    for (const h of ['mac.local', '192.168.1.4', 'example.com']) expect(isLoopbackHost(h), h).toBe(false);
  });
  it('rewrites 0.0.0.0 to loopback in snippets', () => {
    expect(snippetOrigin('http://0.0.0.0:8000')).toBe('http://127.0.0.1:8000');
    expect(snippetOrigin('http://127.0.0.1:8000')).toBe('http://127.0.0.1:8000');
  });
});

describe('SDK snippets', () => {
  const ctx = { origin: 'http://127.0.0.1:8000', model: MODEL, auth: false };
  it('has the seven tabs from docs/ui/09 §5, five with code', () => {
    expect(SDK_TABS).toHaveLength(7);
    expect(SDK_TABS.filter((k) => sdkSnippet(k, ctx) !== null)).toEqual(['openai_py', 'openai_js', 'anthropic_py', 'anthropic_js', 'typesafe']);
  });
  it('fills base URL, model and the local key', () => {
    const py = sdkSnippet('openai_py', ctx)!;
    expect(py).toContain('base_url="http://127.0.0.1:8000/v1"');
    expect(py).toContain('api_key="local"');
    expect(py).toContain(`model="${MODEL}"`);
    expect(sdkSnippet('anthropic_py', ctx)).toContain('base_url="http://127.0.0.1:8000"');
    expect(sdkSnippet('anthropic_js', ctx)).toContain('baseURL: "http://127.0.0.1:8000"');
    expect(sdkSnippet('openai_js', ctx)).toContain('baseURL: "http://127.0.0.1:8000/v1"');
    expect(sdkSnippet('typesafe', ctx)).toContain('TypeSafeClient(');
  });
  it('references SPLASH_API_KEY and never a value when a key is required (D-09-7)', () => {
    const auth = { ...ctx, auth: true };
    expect(sdkSnippet('openai_py', auth)).toContain('os.environ["SPLASH_API_KEY"]');
    expect(sdkSnippet('openai_js', auth)).toContain('process.env.SPLASH_API_KEY');
    expect(sdkSnippet('anthropic_py', auth)).toContain('export SPLASH_API_KEY first');
    for (const k of SDK_TABS) expect(sdkSnippet(k, auth) ?? '').not.toContain('"local"');
  });
  it('mentions how to turn Anthropic thinking on', () => {
    expect(sdkSnippet('anthropic_py', ctx)).toContain('thinking={"type": "enabled"');
  });
});

describe('Tauri origin', () => {
  it('appends exactly tauri://localhost once and notices the wildcard', () => {
    expect(withTauriOrigin(['http://a'])).toEqual(['http://a', 'tauri://localhost']);
    expect(withTauriOrigin(['tauri://localhost'])).toEqual(['tauri://localhost']);
    expect(withTauriOrigin(undefined)).toEqual(['tauri://localhost']);
    expect(tauriOriginState(['*'])).toBe('wildcard');
    expect(tauriOriginState(['tauri://localhost'])).toBe('allowed');
    expect(tauriOriginState(null)).toBe('missing');
  });
});

describe('CLI rows', () => {
  it('parses versions and lists only Hermes/Pi entries', () => {
    expect(parseVersion('codex-cli 0.58.0')).toBe('0.58.0');
    expect(parseVersion('nope')).toBeNull();
    expect(removableEntries({ name: 'hermes', entries: ['splash'] })).toEqual(['splash']);
    expect(removableEntries({ name: 'claude', entries: ['x'] })).toEqual([]);
    expect(entryFile('hermes', 'splash-9000')).toBe('~/.hermes/profiles/splash-9000/config.yaml');
    expect(entryFile('pi', 'splash')).toBe('~/.pi/agent/models.json');
  });
});

describe('desktop rows', () => {
  it('maps detection and state to the row view', () => {
    expect(desktopView({ detected: false, state: 'not_connected' })).toBe('not_detected');
    expect(desktopView({ detected: true, state: 'not_connected' })).toBe('not_connected');
    expect(desktopView({ detected: false, state: 'needs_restore' })).toBe('needs_restore');
    expect(clock('2026-10-03T09:02:00')).toBe('09:02');
    expect(clock(null)).toBeNull();
  });

  it('defaults every slot to the active model and warns when slots split across models', () => {
    const slots = normaliseSlots({ 'claude-opus-5': '' });
    expect(Object.values(slots).every((v) => v === null)).toBe(true);
    expect(Object.keys(slots)).toContain('claude-haiku-4-5-20251001');
    const roots = new Map([[`${MODEL}:no-think`, MODEL]]);
    expect(slotsSwitchModels({ ...slots, 'claude-sonnet-5': `${MODEL}:no-think` }, MODEL, roots)).toBe(false);
    expect(slotsSwitchModels({ ...slots, 'claude-sonnet-5': 'other/model' }, MODEL, roots)).toBe(true);
  });
});
