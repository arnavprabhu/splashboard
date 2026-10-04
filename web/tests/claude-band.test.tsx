import { act, fireEvent, render, screen } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CLAUDE_KEY_REMASK_MS, ClaudeBand } from '../src/routes/status/Bands';
import { toastQueue } from '../src/components/Toast';
import { engine, settings } from '../src/store';

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const MODEL = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL';

beforeEach(() => {
  toastQueue.value = [];
  engine.value = { state: 'ready', model: MODEL } as never;
  settings.value = {
    settings: { version: 1, global: { security: { api_key_required: true } }, models: {} },
  } as never;
});

describe('Status → Claude Code band', () => {
  it('re-masks a revealed API key after 30 s', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/settings/secrets/api-key')) return json(200, { key: 'sk-splash-secret1234' });
      return json(200, { model: MODEL, profiles: [], sampling_defaults: {} });
    });
    render(<ClaudeBand models={[MODEL]} />);
    expect(document.body.textContent).not.toContain('sk-splash-secret1234');
    fireEvent.click(screen.getByRole('button', { name: 'Reveal key' }));
    await vi.waitFor(() => expect(document.body.textContent).toContain('sk-splash-secret1234'));
    await act(async () => {
      vi.advanceTimersByTime(CLAUDE_KEY_REMASK_MS + 10);
    });
    expect(document.body.textContent).not.toContain('sk-splash-secret1234');
    expect(screen.getByRole('button', { name: 'Reveal key' })).toBeTruthy();
  });

  it('reports a failed key read instead of an unhandled rejection', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/settings/secrets/api-key')) return json(503, { error: { message: 'Keychain locked', type: 'overloaded_error', code: 'keychain_unavailable' } });
      return json(200, { model: MODEL, profiles: [], sampling_defaults: {} });
    });
    render(<ClaudeBand models={[MODEL]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Reveal key' }));
    await vi.waitFor(() => expect(toastQueue.value.at(-1)).toMatchObject({ message: 'Couldn’t read the API key.', tone: 'error' }));
    expect(screen.getByRole('button', { name: 'Reveal key' })).toBeTruthy();
  });
});
