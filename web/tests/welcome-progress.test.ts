import { beforeEach, describe, expect, it, vi } from 'vitest';
import { progress, hydrateProgress, resetProgress, updateProgress } from '../src/routes/welcome/frame';
import { markCompleted, saveSettings, saveWizardProgress } from '../src/routes/welcome/api';
import { startSettings } from '../src/routes/welcome/logic';
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

describe('wizard progress, server side', () => {
  it('maps the browser fields to the manager names, and leaves reached and the download in the browser', () => {
    expect(toServerProgress({ ...LOCAL, pendingPort: 18435, preset: 'coding', useCase: 'chat', model: 'a/b' })).toEqual({
      step: 2,
      pending_port: 18435,
      use_case: 'chat',
      model: 'a/b',
    });
  });

  it('sends the step 3 pick as use_case only, never the applied preset (the manager keeps wizard.preset)', () => {
    const body = toServerProgress({ ...LOCAL, preset: 'coding', useCase: null });
    expect(body.use_case).toBeNull();
    expect(body).not.toHaveProperty('preset');
  });

  it('takes the settings values where they are set and keeps the browser copy for the rest', () => {
    const merged = mergeServerProgress(LOCAL, { step: 5, pending_port: 18435, use_case: 'speed', preset: 'chat', model: 'a/b' });
    expect(merged).toMatchObject({ step: 5, reached: 5, pendingPort: 18435, useCase: 'speed', preset: 'chat', model: 'a/b', downloadId: 'd1' });
    expect(mergeServerProgress(LOCAL, { step: null, pending_port: null, use_case: null, model: null })).toEqual(LOCAL);
  });

  it('an applied preset in settings wins over the browser copy, so step 4 recommends for what was applied', () => {
    const local = { ...LOCAL, preset: 'chat' as const };
    expect(mergeServerProgress(local, { preset: 'speed' }).preset).toBe('speed');
    expect(mergeServerProgress(local, { preset: null }).preset).toBe('chat');
  });

  it('never moves reached backwards and ignores values the manager would refuse', () => {
    const merged = mergeServerProgress(LOCAL, { step: 1, pending_port: 70000, use_case: 'fast' as never, preset: 'fast' as never, model: '' });
    expect(merged.step).toBe(1);
    expect(merged.reached).toBe(3);
    expect(merged.pendingPort).toBeNull();
    expect(merged.useCase).toBeNull();
    expect(merged.preset).toBeNull();
    expect(merged.model).toBeNull();
    expect(mergeServerProgress(LOCAL, undefined)).toBe(LOCAL);
  });

  it('a stored record that is broken reads as the empty progress', () => {
    expect(readProgress({ step: 9, pendingPort: 'x' })).toMatchObject({ step: 1, pendingPort: null, useCase: null });
  });

  it('a stored step 3 pick survives a reload as the unapplied choice, apart from the applied preset', () => {
    const read = readProgress({ step: 3, reached: 3, preset: 'coding', useCase: 'speed' });
    expect(read).toMatchObject({ preset: 'coding', useCase: 'speed' });
    expect(readProgress({ useCase: 'fast' })).toMatchObject({ useCase: null });
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
    expect(progress.value).toMatchObject({ step: 1, pendingPort: null, model: null, preset: null, useCase: null });
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

describe('step 5 settings write', () => {
  it('the loaded model becomes routing.default_model and the other routing keys stay', () => {
    const doc = { global: { routing: { auto_load: false, default_model: 'a/old' } } } as { global: Record<string, unknown> };
    startSettings(doc, 'a/new', null);
    expect(doc.global.routing).toEqual({ auto_load: false, default_model: 'a/new' });
    expect(doc.global).not.toHaveProperty('server');
  });

  it('a port typed in step 2 is applied, and the server keeps its host', () => {
    const doc = { global: { server: { host: '0.0.0.0', port: 8000 } } } as { global: Record<string, unknown> };
    startSettings(doc, 'a/b', 18435);
    expect(doc.global.server).toEqual({ host: '0.0.0.0', port: 18435 });
  });

  it('with no model in progress the default is left alone', () => {
    const doc = { global: { routing: { default_model: 'a/keep' } } } as { global: Record<string, unknown> };
    startSettings(doc, null, null);
    expect(doc.global.routing).toEqual({ default_model: 'a/keep' });
  });

  it('finishing setup PUTs the default model and the port with the rest of the document', async () => {
    const doc = { version: 1, global: { wizard: { completed: false, preset: 'coding', model: 'a/b' }, routing: { auto_load: true } }, models: {} };
    const puts: { global: Record<string, Record<string, unknown>> }[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if (!String(input).endsWith('/settings')) return json(200, {});
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)));
        return json(200, { restart_required: false, applied: {} });
      }
      return json(200, { settings: JSON.parse(JSON.stringify(doc)), secrets: {}, resolved: {} });
    });
    await markCompleted((d) => startSettings(d, 'a/b', 18435));
    expect(puts).toHaveLength(1);
    expect(puts[0]?.global).toMatchObject({ routing: { auto_load: true, default_model: 'a/b' }, server: { port: 18435 }, wizard: { completed: true, preset: 'coding' } });
  });
});
