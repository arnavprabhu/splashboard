/**
 * Admin sign-in against the real manager: one-time links, cross-site
 * writes, logout, and sign-in off (reads open, writes need a session).
 */
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { expect, FAKE_MODEL, test } from '../support/manager';

test.use({ managerOptions: { signedIn: false } });

test.describe('admin sign-in', () => {
  test('a one-time link signs the browser in and lands on next; the link works once', async ({ page, manager }) => {
    const link = await manager.mintLink();
    expect(link).toMatch(/^\/admin\/login\?code=[\w-]{20,}$/);
    await page.goto(`${link}&next=${encodeURIComponent('/admin/settings/security')}`);
    await expect(page).toHaveURL(/\/admin\/settings\/security$/);
    expect(page.url()).not.toContain('code=');
    await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
    await expect(page.getByTestId('security-lan-tls')).toContainText('unencrypted');
    const cookies = await page.context().cookies();
    const session = cookies.find((c) => c.name === 'splash_gui_session');
    expect(session?.httpOnly).toBe(true);
    expect(session?.sameSite).toBe('Strict');

    // The same code again, in a fresh browser: refused, with the expired message and the key form.
    const other = await page.context().browser()!.newContext({ baseURL: manager.url });
    const second = await other.newPage();
    await second.goto(link);
    await expect(second.getByTestId('login-link-error')).toContainText('expired or was already used');
    await expect(second.getByLabel('API key', { exact: true })).toBeVisible();
    expect(second.url()).not.toContain('code=');
    await other.close();
  });

  test('without a session, pages go to sign in and come back after the API key', async ({ page, manager }) => {
    const key = await manager.api<{ key: string }>('GET', '/settings/secrets/api-key');
    expect(key.status).toBe(200);
    await page.goto('/admin/models');
    await expect(page).toHaveURL(/\/admin\/login\?next=%2Fmodels$/);
    await page.getByLabel('API key', { exact: true }).fill('not-the-key');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByText('That key didn’t match.')).toBeVisible();
    await page.getByLabel('API key', { exact: true }).fill(key.body.key);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page).toHaveURL(/\/admin\/models$/);
  });

  test('logout revokes the session: a reload asks to sign in again', async ({ page, manager }) => {
    await manager.signIn(page.context());
    await page.goto('/admin/status');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    const before = (await page.context().cookies()).find((c) => c.name === 'splash_gui_session');
    expect(before).toBeTruthy();
    await page.getByRole('button', { name: 'Log out' }).click();
    await expect(page).toHaveURL(/\/admin\/login/);
    await page.goto('/admin/status');
    await expect(page).toHaveURL(/\/admin\/login\?next=%2Fstatus$/);
    // Replaying the old cookie does not help: the session was revoked, not only cleared.
    await page.context().addCookies([before!]);
    const replay = await page.request.get('/api/admin/engine');
    expect(replay.status()).toBe(401);
    expect((await replay.json()).error.code).toBe('auth_required');
  });

  test.describe('cross-site', () => {
    let attacker: Server;
    let origin = '';
    test.beforeAll(async () => {
      attacker = createServer((_req, res) => {
        res.setHeader('Content-Type', 'text/html');
        res.end('<!doctype html><title>elsewhere</title><p>elsewhere</p>');
      });
      await new Promise<void>((ok) => attacker.listen(0, '127.0.0.1', ok));
      // localhost vs 127.0.0.1 makes it cross-site, not only cross-origin.
      origin = `http://localhost:${(attacker.address() as AddressInfo).port}`;
    });
    test.afterAll(() => new Promise<void>((ok) => attacker.close(() => ok())));

    test.use({ managerOptions: { signedIn: false, installed: [FAKE_MODEL] } });

    test('a forged cross-site write is refused even with a signed-in browser', async ({ page, manager }) => {
      await manager.signIn(page.context());
      await page.goto('/admin/status');
      await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
      const before = await manager.api<{ settings: { global: { server: { allowed_hosts: string[] } } } }>('GET', '/settings');

      await page.goto(origin);
      const statuses: Record<string, number> = {};
      page.on('response', (r) => {
        const u = new URL(r.url());
        if (u.pathname.startsWith('/api/admin/')) statuses[`${r.request().method()} ${u.pathname}`] = r.status();
      });
      await page.evaluate(
        async ({ target, model }) => {
          const send = (path: string, body: unknown) =>
            fetch(`${target}/api/admin${path}`, {
              method: 'POST',
              mode: 'no-cors',
              credentials: 'include',
              headers: { 'Content-Type': 'text/plain' },
              body: JSON.stringify(body),
            }).catch(() => undefined);
          await send('/engine/load', { model });
          await send('/auth/link', {});
          // A classic form post, as a page would do it without script.
          const form = document.createElement('form');
          form.method = 'POST';
          form.action = `${target}/api/admin/doctor`;
          form.target = 'sink';
          const sink = document.createElement('iframe');
          sink.name = 'sink';
          document.body.append(sink, form);
          form.submit();
          await new Promise((r) => setTimeout(r, 500));
        },
        { target: manager.url, model: FAKE_MODEL },
      );
      await expect.poll(() => Object.keys(statuses).length).toBeGreaterThanOrEqual(3);
      for (const [call, status] of Object.entries(statuses)) expect([401, 403], call).toContain(status);

      const engine = await manager.api<{ state: string }>('GET', '/engine');
      expect(engine.body.state).toBe('stopped');
      const after = await manager.api('GET', '/settings');
      expect(after.body).toEqual(before.body);
    });
  });

  test('with sign-in off, reads stay open but a save asks to sign in and returns to the page', async ({ page, manager }) => {
    await manager.patchSettings((doc) => {
      doc.global.security = { ...doc.global.security, admin_requires_key: false };
    });
    const key = (await manager.api<{ key: string }>('GET', '/settings/secrets/api-key')).body.key;
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/settings/lifecycle');
    await expect(page).toHaveURL(/\/admin\/settings\/lifecycle$/);
    await expect(page.getByRole('button', { name: 'Log out' })).toHaveCount(0);
    const field = page.locator('[data-key="lifecycle.auto_restart"]');
    await field.getByRole('switch').click();
    await page.getByRole('button', { name: /^Save/ }).click();
    await expect(page).toHaveURL(/\/admin\/login\?next=%2Fsettings%2Flifecycle$/);
    await expect(page.getByText(/Reading is open on this Mac/)).toBeVisible();
    await page.getByLabel('API key', { exact: true }).fill(key);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page).toHaveURL(/\/admin\/settings\/lifecycle$/);
    // The edit survived the detour; saving now works.
    await page.getByRole('button', { name: /^Save/ }).click();
    await expect.poll(async () => {
      const got = await manager.api<{ settings: { global: { lifecycle: { auto_restart: boolean } } } }>('GET', '/settings');
      return got.body.settings.global.lifecycle.auto_restart;
    }).toBe(false);
  });
});

test.describe('fresh install sign-in (acceptance 2026-10-07)', () => {
  test.use({ managerOptions: { signedIn: false, wizardCompleted: false } });

  test('signed out, /admin asks to sign in and a link lands on the welcome wizard', async ({ page, manager }) => {
    await page.goto('/admin/');
    await expect(page).toHaveURL(/\/admin\/login\?next=%2F$/);
    // `splash open` with no page, or a link without next, also returns to the root.
    await page.goto(await manager.mintLink());
    await expect(page).toHaveURL(/\/admin\/welcome/);
    await expect(page.getByText('Step 1 of 5 · Engine')).toBeVisible();
  });
});

