/**
 * Sign-in (D58, docs/ui/01 §6): one-time links (`?code=`), the API-key form, and the 401
 * `auth_required` redirect that returns the user to the page they were on, which applies to
 * writes even while admin sign-in is off.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../src/api/client';
import { App, loginRedirect } from '../src/app';
import { Router } from 'wouter-preact';
import { ErrorBanner } from '../src/routes/chat/ErrorBanner';
import { classifyError } from '../src/routes/chat/logic';
import { ApiError } from '../src/api/client';
import { stripCode } from '../src/routes/login';
import { auth, authKnown, managerReachable, settings, settingsLoaded, startStore } from '../src/store';

function json(status: number, body: unknown): Response {
  return new Response(body === null ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const AUTH_REQUIRED = { error: { message: 'Sign in to Splash GUI', type: 'authentication_error', code: 'auth_required' } };
const SESSION = { admin_requires_key: true, authenticated: true, method: 'session' };

type Handler = (method: string, path: string, body: string | undefined) => Response | undefined;

/** A fetch stub keyed by "METHOD /path"; unmatched calls answer `{}`. Records every call. */
function stubFetch(handler: Handler) {
  const calls: Array<{ method: string; path: string; body: string | undefined }> = [];
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = new URL(String(input), 'http://127.0.0.1');
    const method = init?.method ?? 'GET';
    const body = typeof init?.body === 'string' ? init.body : undefined;
    calls.push({ method, path: url.pathname, body });
    return handler(method, url.pathname, body) ?? json(200, {});
  });
  return { calls, spy };
}

function go(path: string) {
  history.replaceState(null, '', path);
}

beforeEach(() => {
  settings.value = null;
  settingsLoaded.value = false;
  managerReachable.value = true;
  auth.value = { admin_requires_key: true, authenticated: false, method: null };
  authKnown.value = true;
});

describe('stripCode', () => {
  it('drops only the code', () => {
    expect(stripCode('http://127.0.0.1:8140/admin/login?code=abc&next=%2Fsettings%2Fsecurity')).toBe(
      '/admin/login?next=%2Fsettings%2Fsecurity',
    );
    expect(stripCode('http://127.0.0.1:8140/admin/login?code=abc')).toBe('/admin/login');
  });
});

describe('one-time sign-in link', () => {
  it('exchanges the code, strips it from the address bar and lands on next', async () => {
    let state: unknown = { admin_requires_key: true, authenticated: false, method: null };
    const { calls } = stubFetch((_method, path) => {
      if (path === '/api/admin/auth/exchange') {
        state = SESSION;
        return json(200, SESSION);
      }
      if (path === '/api/admin/auth/state') return json(200, state);
      return undefined;
    });
    go('/admin/login?code=one-time-code&next=%2Fsettings%2Fsecurity');
    const lengthBefore = history.length;
    render(<App base="/admin" />);
    await waitFor(() => expect(location.pathname).toBe('/admin/settings/security'));
    const exchange = calls.filter((c) => c.path === '/api/admin/auth/exchange');
    expect(exchange).toHaveLength(1);
    expect(exchange[0]!.method).toBe('POST');
    expect(JSON.parse(exchange[0]!.body!)).toEqual({ code: 'one-time-code' });
    expect(location.href).not.toContain('one-time-code');
    // The code was replaced, not pushed: Back does not return to it.
    expect(history.length).toBe(lengthBefore);
    expect(auth.value.method).toBe('session');
  });

  it('says an expired or spent link has expired and keeps the key form', async () => {
    stubFetch((_method, path) =>
      path === '/api/admin/auth/exchange'
        ? json(401, { error: { message: 'This sign-in link has expired', type: 'authentication_error', code: 'invalid_code' } })
        : undefined,
    );
    go('/admin/login?code=stale&next=%2Fmodels');
    render(<App base="/admin" />);
    expect((await screen.findByTestId('login-link-error')).textContent).toContain('expired or was already used');
    expect(location.search).toBe('?next=%2Fmodels');
    expect(screen.getByLabelText('API key')).toBeTruthy();
    expect(location.pathname).toBe('/admin/login');
  });

  it('a spent code is not exchanged twice', async () => {
    const { calls } = stubFetch((_m, path) => (path === '/api/admin/auth/exchange' ? json(401, { error: { message: 'gone', type: 'authentication_error', code: 'invalid_code' } }) : undefined));
    go('/admin/login?code=twice');
    const first = render(<App base="/admin" />);
    await screen.findByTestId('login-link-error');
    first.unmount();
    go('/admin/login?code=twice');
    render(<App base="/admin" />);
    await screen.findByLabelText('API key');
    expect(calls.filter((c) => c.path === '/api/admin/auth/exchange')).toHaveLength(1);
  });
});

describe('401 auth_required', () => {
  it('a write refused while sign-in is off sends the user to sign in and back', async () => {
    // What the manager reports with sign-in off and no credential (docs/api.md §12.5).
    auth.value = { admin_requires_key: false, authenticated: false, method: 'open' };
    let signedIn = false;
    stubFetch((method, path) => {
      if (path === '/api/admin/auth/login') {
        signedIn = true;
        return json(200, { admin_requires_key: false, authenticated: true, method: 'session' });
      }
      if (method === 'PUT' && path === '/api/admin/settings' && !signedIn) return json(401, AUTH_REQUIRED);
      return undefined;
    });
    const stop = startStore((expired) => loginRedirect(expired, '/admin'));
    go('/admin/settings/security');
    render(<App base="/admin" />);
    await expect(api.put('/settings', {})).rejects.toMatchObject({ status: 401, code: 'auth_required' });
    await waitFor(() => expect(location.pathname).toBe('/admin/login'));
    expect(new URLSearchParams(location.search).get('next')).toBe('/settings/security');
    // Sign-in is off: the page says why it is asking.
    expect(await screen.findByText(/Reading is open on this Mac/)).toBeTruthy();
    fireEvent.input(await screen.findByLabelText('API key'), { target: { value: 'sk-test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(location.pathname).toBe('/admin/settings/security'));
    await expect(api.put('/settings', {})).resolves.toEqual({});
    stop();
  });

  it('a wrong key at login does not bounce, and other 401 codes leave the page alone', async () => {
    const handler = vi.fn();
    stubFetch(() => json(401, { error: { message: 'bad', type: 'authentication_error', code: 'invalid_key' } }));
    const { setUnauthorizedHandler } = await import('../src/api/client');
    setUnauthorizedHandler(handler);
    await expect(api.post('/auth/login', { key: 'x' })).rejects.toMatchObject({ code: 'invalid_key' });
    expect(handler).not.toHaveBeenCalled();
    setUnauthorizedHandler(null);
  });
});

describe('logout', () => {
  it('revokes the session and shows the sign-in page', async () => {
    auth.value = { ...SESSION, method: 'session' };
    let state: unknown = SESSION;
    const { calls } = stubFetch((_m, path) => {
      if (path === '/api/admin/auth/logout') {
        state = { admin_requires_key: true, authenticated: false, method: null };
        return json(204, null);
      }
      if (path === '/api/admin/auth/state') return json(200, state);
      return undefined;
    });
    go('/admin/status');
    render(<App base="/admin" />);
    fireEvent.click(await screen.findByText('Log out'));
    await waitFor(() => expect(location.pathname).toBe('/admin/login'));
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/admin/auth/logout')).toBe(true);
    expect(await screen.findByLabelText('API key')).toBeTruthy();
    expect(auth.value.authenticated).toBe(false);
  });

  it('offers Log out with sign-in off once a session exists', async () => {
    auth.value = { admin_requires_key: false, authenticated: true, method: 'session' };
    stubFetch(() => undefined);
    go('/admin/status');
    render(<App base="/admin" />);
    expect(await screen.findByText('Log out')).toBeTruthy();
  });
});

describe('Chat /v1 401 with sign-in off (D58)', () => {
  it('offers Sign in, returning to this chat', async () => {
    go('/admin/chat/c-123?model=a%2Fb');
    const error = classifyError(new ApiError(401, { message: 'invalid or missing API key', type: 'authentication_error', code: 'authentication_error' }));
    const noop = () => undefined;
    render(
      <Router base="/admin">
        <ErrorBanner
          error={{ ...error, retries: 0, retryIn: null }}
          onRetry={noop}
          onSwitchWhenIdle={noop}
          onOpenTab={noop}
          onTurnOffEos={noop}
          onRemoveAttachments={noop}
          onShowRequest={noop}
          onDismiss={noop}
        />
      </Router>,
    );
    const link = await screen.findByRole('link', { name: 'Sign in' });
    expect(link.getAttribute('href')).toBe('/admin/login?next=%2Fchat%2Fc-123%3Fmodel%3Da%252Fb');
    expect(screen.getByText('Sign in to chat.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });
});
