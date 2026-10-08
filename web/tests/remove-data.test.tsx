/**
 * Settings → About → Remove Splash GUI data… (SPEC §19, PKG-12): the plan's sizes, models and cache
 * unticked by default, the typed DELETE they need, the request body, and the menu bar app case.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RemoveData } from '../src/routes/settings/RemoveData';

const PLAN = {
  home: '/Users/u/.splash',
  items: [
    { path: '/Users/u/.splash/chats', kind: 'data', bytes: 2048, deletable: true },
    { path: '/Users/u/.splash/models', kind: 'models', bytes: 61.2e9, deletable: true },
    { path: '/Users/u/.splash/cache', kind: 'cache', bytes: 9.6e9, deletable: true },
    { path: '/Volumes/big/hf', kind: 'models', bytes: 1e9, deletable: false },
  ],
  models_bytes: 62.2e9,
  cache_bytes: 9.6e9,
  data_bytes: 2048,
  connected_integrations: [],
  path_block_files: [],
  shim_installed: true,
  app_connected: false,
  steps: ['Restore Claude Desktop and the Codex app', 'Stop the manager'],
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function stubFetch(plan: unknown) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith('/uninstall/plan')) return json(200, plan);
    if (url.endsWith('/uninstall')) {
      return json(200, {
        restored: [],
        path_block_removed: [],
        shim_removed: true,
        secrets_deleted: 2,
        deleted: ['/Users/u/.splash/chats'],
        kept: [],
        freed_bytes: 2048,
        stopping: true,
      });
    }
    return json(404, {});
  });
}

function sentBody(spy: { mock: { calls: unknown[][] } }): Record<string, unknown> | undefined {
  const call = spy.mock.calls.find(([input]) => String(input).endsWith('/uninstall'));
  return call ? JSON.parse(String((call[1] as RequestInit).body)) : undefined;
}

afterEach(() => vi.restoreAllMocks());

describe('Remove Splash GUI data', () => {
  it('lists the sizes with models and cache unticked, and removes only the data by default', async () => {
    const spy = stubFetch(PLAN);
    render(<RemoveData />);
    fireEvent.click(screen.getByTestId('remove-data'));
    const models = await screen.findByRole('checkbox', { name: /Delete models \(/ });
    const cache = screen.getByRole('checkbox', { name: /Delete cache \(/ });
    expect((models as HTMLInputElement).checked).toBe(false);
    expect((cache as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(/Kept: \/Volumes\/big\/hf is outside the data folder/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await screen.findByText(/The manager has stopped/);
    expect(sentBody(spy)).toEqual({ delete_data: true, delete_models: false, delete_cache: false, stop: true });
  });

  it('needs the typed DELETE once models or the cache are ticked', async () => {
    const spy = stubFetch(PLAN);
    render(<RemoveData />);
    fireEvent.click(screen.getByTestId('remove-data'));
    fireEvent.click(await screen.findByRole('checkbox', { name: /Delete models \(/ }));
    const remove = screen.getByRole('button', { name: 'Remove' }) as HTMLButtonElement;
    expect(remove.disabled).toBe(true);
    fireEvent.input(screen.getByLabelText('Type DELETE to delete models or the cache'), { target: { value: 'DELETE' } });
    await waitFor(() => expect(remove.disabled).toBe(false));
    fireEvent.click(remove);
    await screen.findByText(/The manager has stopped/);
    expect(sentBody(spy)?.delete_models).toBe(true);
  });

  it('sends the user to the menu bar app when it is connected', async () => {
    stubFetch({ ...PLAN, app_connected: true });
    render(<RemoveData />);
    fireEvent.click(screen.getByTestId('remove-data'));
    expect(await screen.findByText(/Use About → Remove Splash GUI Data… in the app/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Remove' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
