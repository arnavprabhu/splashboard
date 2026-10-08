import { beforeEach, describe, expect, it, vi } from 'vitest';
import { progress, hydrateProgress, resetProgress, updateProgress } from '../src/routes/welcome/frame';
import { saveSettings, saveWizardProgress } from '../src/routes/welcome/api';
import { EMPTY_PROGRESS, mergeServerProgress, readProgress, toServerProgress, type WizardProgress } from '../src/routes/welcome/steps';
import { settings } from '../src/store';

const LOCAL: WizardProgress = { ...EMPTY_PROGRESS, step: 2, reached: 3, pendingPort: null, preset: null, model: null, downloadId: 'd1' };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

beforeEach(() => {
  localStorage.clear();
  progress.value = { ...EMPTY_PROGRESS };
  settings.value = { settings: { version: 1, global: {}, models: {} } } as never;
});

describe('settings writes', () => {
  it('two read-modify-writes at once keep both edits, because they run one after another', async () => {
    let stored: { version: number; global: Record<string, unknown>; models: Record<string, unknown> } = { version: 1, global: {}, models: {} };
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (!url.endsWith('/settings')) return json(200, {});
      if (init?.method === 'PUT') {
        stored = JSON.parse(String(init.body));
        return json(200, { restart_required: false, applied: {} });
      }
      return json(200, { settings: JSON.parse(JSON.stringify(stored)), secrets: {}, resolved: {} });
    });
    await Promise.all([
      saveSettings((doc) => {
        doc.global.serve = { queue_size: 4 } as never;
      }),
      saveSettings((doc) => {
        doc.global.routing = { default_model: 'a/b' } as never;
      }),
    ]);
    expect(stored.global).toMatchObject({ serve: { queue_size: 4 }, routing: { default_model: 'a/b' } });
  });
});

describe('wizard progress, server side (F3)', () => {
  it('maps the browser fields to the manager names, and leaves reached and the download in the browser', () => {
    expect(toServerProgress({ ...LOCAL, pendingPort: 18435, preset: 'chat', model: 'a/b' })).toEqual({
      step: 2,
      pending_port: 18435,
      use_case: 'chat',
      model: 'a/b',
    });
  });

  it('takes the settings values where they are set and keeps the browser copy for the rest', () => {
    const merged = mergeServerProgress(LOCAL, { step: 5, pending_port: 18435, use_case: 'speed', model: 'a/b' });
    expect(merged).toMatchObject({ step: 5, reached: 5, pendingPort: 18435, preset: 'speed', model: 'a/b', downloadId: 'd1' });
    expect(mergeServerProgress(LOCAL, { step: null, pending_port: null, use_case: null, model: null })).toEqual(LOCAL);
  });

  it('never moves reached backwards and ignores values the manager would refuse', () => {
    const merged = mergeServerProgress(LOCAL, { step: 1, pending_port: 70000, use_case: 'fast' as never, model: '' });
    expect(merged.step).toBe(1);
    expect(merged.reached).toBe(3);
    expect(merged.pendingPort).toBeNull();
    expect(merged.preset).toBeNull();
    expect(merged.model).toBeNull();
    expect(mergeServerProgress(LOCAL, undefined)).toBe(LOCAL);
  });

  it('a stored record that is broken reads as the empty progress', () => {
    expect(readProgress({ step: 9, pendingPort: 'x' })).toMatchObject({ step: 1, pendingPort: null });
  });

  it('hydrating takes the port typed in step 2 from settings, which step 5 then applies', () => {
    localStorage.setItem('splash-gui-wizard', JSON.stringify(LOCAL));
    progress.value = readProgress(JSON.parse(localStorage.getItem('splash-gui-wizard')!));
    settings.value = {
      settings: { version: 1, global: { wizard: { completed: false, step: 5, pending_port: 18435, use_case: 'chat', model: 'a/b' } }, models: {} },
    } as never;
    hydrateProgress();
    expect(progress.value).toMatchObject({ step: 5, pendingPort: 18435, model: 'a/b', downloadId: 'd1' });
  });

  it('PUTs the wizard fields with the rest of the document and keeps the other wizard keys', async () => {
    const doc = { version: 1, global: { wizard: { completed: false, preset: 'coding' } }, models: {} };
    const puts: unknown[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (!url.endsWith('/settings')) return json(200, {});
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)));
        return json(200, { restart_required: false, applied: {} });
      }
      return json(200, { settings: doc, secrets: {}, resolved: {} });
    });
    await saveWizardProgress({ step: 2, pending_port: 18435, use_case: null, model: null });
    expect(puts).toHaveLength(1);
    expect(puts[0]).toMatchObject({ global: { wizard: { completed: false, preset: 'coding', step: 2, pending_port: 18435 } } });
  });

  it('after setup is finished, a later step change does not write the old step and port back', async () => {
    progress.value = { ...EMPTY_PROGRESS, step: 5, reached: 5, pendingPort: 18435, preset: 'coding', model: 'a/b' };
    const doc = { version: 1, global: { wizard: { completed: true, preset: 'coding' } }, models: {} };
    const puts: { global: { wizard?: Record<string, unknown> } }[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (!url.endsWith('/settings')) return json(200, {});
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)));
        return json(200, { restart_required: false, applied: {} });
      }
      return json(200, { settings: doc, secrets: {}, resolved: {} });
    });
    // What StepStart's completion does after markCompleted succeeds.
    resetProgress();
    expect(progress.value).toMatchObject({ step: 1, pendingPort: null, model: null, preset: null });
    updateProgress({ step: 2 });
    await vi.waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]?.global.wizard).toMatchObject({ completed: true, step: 2, pending_port: null, use_case: null, model: null });
  });

  it('a step change writes the local copy first, and a refused write leaves it in place', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(409, { error: { message: 'read only', type: 'x', code: 'settings_read_only' } }));
    updateProgress({ step: 3, reached: 3 });
    expect(JSON.parse(localStorage.getItem('splash-gui-wizard')!)).toMatchObject({ step: 3, reached: 3 });
    await new Promise((r) => setTimeout(r, 0));
    expect(progress.value.step).toBe(3);
  });
});
