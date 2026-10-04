import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/app';
import { ROUTES } from '../src/routes';
import { auth, authKnown, managerReachable, settings, settingsLoaded } from '../src/store';

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

beforeEach(() => {
  settings.value = null;
  settingsLoaded.value = false;
  managerReachable.value = null;
  auth.value = { admin_requires_key: false, authenticated: true, method: null };
  authKnown.value = false;
});

function go(path: string) {
  history.replaceState(null, '', path);
}

describe('App shell', () => {
  it('redirects /admin to /admin/status and lazy-loads the page', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('offline'));
    go('/admin');
    render(<App base="/admin" />);
    await waitFor(() => expect(location.pathname).toBe('/admin/status'));
    expect(await screen.findByRole('heading', { level: 1 })).toBeTruthy();
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeTruthy();
  });

  it('decodes model settings paths with OWNER/REPO:VARIANT', async () => {
    go('/admin/models/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL/settings');
    render(<App base="/admin" />);
    expect(await screen.findByText('unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL')).toBeTruthy();
  });

  it('sends a first run to the welcome wizard', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json(200, { settings: { version: 1, global: { wizard: { completed: false } }, models: {} } }),
    );
    go('/admin/');
    render(<App base="/admin" />);
    await waitFor(() => expect(location.pathname).toBe('/admin/welcome'));
  });

  it('redirects /admin/tools to the Playground tab', async () => {
    go('/admin/tools');
    render(<App base="/admin" />);
    await waitFor(() => expect(location.pathname).toBe('/admin/tools/playground'));
    expect((await screen.findByText('Playground')).getAttribute('aria-current')).toBe('page');
  });

  it('renders bare pages without the main nav', async () => {
    go('/admin/login');
    render(<App base="/admin" />);
    expect(await screen.findByText('Sign in.')).toBeTruthy();
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull();
  });

  it('signs in and returns to ?next', async () => {
    auth.value = { admin_requires_key: true, authenticated: false, method: null };
    authKnown.value = true;
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json(401, { error: { message: 'invalid key', type: 'authentication_error', code: 'invalid_key' } }))
      .mockResolvedValue(json(200, { admin_requires_key: true, authenticated: true, method: 'session' }));
    go('/admin/login?next=%2Fmodels%2Fdownloader');
    render(<App base="/admin" />);
    const input = await screen.findByLabelText('Admin key');
    fireEvent.input(input, { target: { value: 'wrong' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('That key didn’t match.')).toBeTruthy();
    fireEvent.input(input, { target: { value: 'right' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(location.pathname).toBe('/admin/models/downloader'));
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe('{"key":"right"}');
    expect(auth.value.method).toBe('session');
  });

  it('leaves the login page when auth is off', async () => {
    authKnown.value = true;
    go('/admin/login?next=%2Fchat');
    render(<App base="/admin" />);
    await waitFor(() => expect(location.pathname).toBe('/admin/chat'));
  });

  it('shows Log out only for a session login and an offline notice when the manager is down', async () => {
    auth.value = { admin_requires_key: true, authenticated: true, method: 'session' };
    managerReachable.value = false;
    go('/admin/status');
    render(<App base="/admin" />);
    expect(await screen.findByText('Log out')).toBeTruthy();
    expect(screen.getByTestId('offline-screen')).toBeTruthy();
  });

  it('shows the not-found page for unknown routes', async () => {
    go('/admin/nope');
    render(<App base="/admin" />);
    expect(await screen.findByText('Not found.')).toBeTruthy();
  });

  it('declares every route from the build plan', () => {
    const paths = ROUTES.map((r) => r.path);
    for (const p of [
      '/status',
      '/status/history',
      '/models',
      '/models/downloader',
      '/models/:id/settings',
      '/chat',
      '/chat/:cid',
      '/tools/playground',
      '/tools/tokenizer',
      '/tools/judgments',
      '/tools/benchmark',
      '/integrations',
      '/logs',
      '/logs/diagnostics',
      '/settings',
      '/settings/:section',
      '/welcome',
      '/login',
      '/_design',
    ]) {
      expect(paths).toContain(p);
    }
  });
});
