import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

/** Manager-shaped answers (manager/splash_gui/schemas.py) for a stopped engine with auth off. */
const ENGINE = {
  state: 'stopped',
  model: null,
  since: '2026-10-03T12:00:00+00:00',
  restart: { auto_restart: true },
  engine: { found: true, version: '1.2.0', support: 'supported' },
};
const SETTINGS = { settings: { version: 1, global: { ui: { theme: 'light' }, wizard: { completed: true } }, models: {} } };

interface MockOptions {
  alerts?: unknown[];
  auth?: { admin_requires_key: boolean; authenticated: boolean; method: string | null };
}

async function mockManager(page: Page, opts: MockOptions = {}) {
  const calls: string[] = [];
  const auth = { ...(opts.auth ?? { admin_requires_key: false, authenticated: true, method: 'open' }) };
  await page.route('**/api/admin/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace('/api/admin', '');
    calls.push(`${req.method()} ${path}`);
    if (path === '/auth/state') return route.fulfill({ json: auth });
    if (path === '/auth/login') {
      const ok = (req.postDataJSON() as { key?: string }).key === 'secret';
      if (!ok) return route.fulfill({ status: 401, json: { error: { message: 'invalid key', type: 'authentication_error', code: 'invalid_key' } } });
      auth.authenticated = true;
      auth.method = 'session';
      return route.fulfill({ json: auth });
    }
    if (auth.admin_requires_key && !auth.authenticated) {
      return route.fulfill({ status: 401, json: { error: { message: 'Sign in required', type: 'authentication_error', code: 'unauthorized' } } });
    }
    if (path === '/engine') return route.fulfill({ json: ENGINE });
    if (path === '/settings') return route.fulfill({ json: SETTINGS });
    if (path === '/alerts') return route.fulfill({ json: { alerts: opts.alerts ?? [] } });
    if (path.endsWith('/dismiss') || path === '/engine/restart') return route.fulfill({ json: { ok: true } });
    return route.fulfill({ status: 501, json: { error: { message: 'not in smoke tests', type: 'not_implemented', code: 'not_implemented' } } });
  });
  return calls;
}

const ROUTES = [
  '/admin/status',
  '/admin/status/history',
  '/admin/models',
  '/admin/models/downloader',
  '/admin/models/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL/settings',
  '/admin/chat',
  '/admin/tools/playground',
  '/admin/tools/tokenizer',
  '/admin/tools/judgments',
  '/admin/tools/benchmark',
  '/admin/integrations',
  '/admin/logs',
  '/admin/logs/diagnostics',
  '/admin/settings/cache',
  '/admin/welcome',
  '/admin/login',
  '/admin/_design',
];

test.describe('shell', () => {
  test('renders the nav band and the Status route', async ({ page }) => {
    await mockManager(page);
    await page.goto('/admin/');
    await expect(page).toHaveURL(/\/admin\/status$/);
    await expect(page.getByRole('navigation', { name: 'Main' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Status', exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.locator('.navband').getByText('Splash 1.2.0')).toBeVisible();
    await expect(page.locator('.navband .chip')).toHaveAttribute('data-live', 'false');
  });

  test('deep links work and the Tools sub-band shows', async ({ page }) => {
    await mockManager(page);
    await page.goto('/admin/tools/judgments');
    await expect(page.getByRole('navigation', { name: 'Tools' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Judgments' })).toHaveAttribute('aria-current', 'page');
  });

  test('theme toggles, persists across reloads, and applies before paint', async ({ page }) => {
    await mockManager(page);
    await page.goto('/admin/status');
    const html = page.locator('html');
    await expect(html).toHaveAttribute('data-theme', 'light');
    const toggle = page.getByTestId('theme-toggle');
    await expect(toggle).toHaveText('☾ Dark');
    await toggle.click();
    await expect(html).toHaveAttribute('data-theme', 'dark');
    await expect(toggle).toHaveText('☀ Light');
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    expect(bg).toBe('rgb(14, 14, 14)');
    // The inline script must set the theme before any module script runs.
    await page.route('**/assets/index-*.js', (route) => route.abort());
    await page.reload();
    await expect(html).toHaveAttribute('data-theme', 'dark');
  });

  test('no element has a border radius or box shadow', async ({ page }) => {
    await mockManager(page, {
      alerts: [{ id: 'queue_full', severity: 'warn', message: 'Queue full (32)', actions: [] }],
    });
    for (const path of ['/admin/status', '/admin/_design', '/admin/login']) {
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      const offenders = await page.evaluate(() => {
        const bad: string[] = [];
        const check = (el: Element, pseudo: string | null) => {
          const s = getComputedStyle(el, pseudo);
          const radius = [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomLeftRadius, s.borderBottomRightRadius];
          const gradient = s.backgroundImage.includes('gradient');
          if (radius.some((r) => r !== '0px') || (s.boxShadow !== 'none' && s.boxShadow !== '') || gradient) {
            bad.push(`${el.tagName.toLowerCase()}.${el.className}${pseudo ?? ''}`);
          }
        };
        for (const el of Array.from(document.querySelectorAll('*'))) {
          check(el, null);
          check(el, '::before');
          check(el, '::after');
        }
        return bad;
      });
      expect(offenders, `on ${path}`).toEqual([]);
    }
  });

  test('no horizontal scroll at phone width on any route', async ({ page }) => {
    await mockManager(page);
    await page.setViewportSize({ width: 360, height: 780 });
    for (const path of ROUTES) {
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `on ${path}`).toBeLessThanOrEqual(0);
    }
  });

  test('alerts: most severe first, Dismiss only when allowed, actions call the manager', async ({ page }) => {
    const calls = await mockManager(page, {
      alerts: [
        { id: 'write_behind_refused', severity: 'info', message: 'Persistent cache paused', actions: [] },
        {
          id: 'engine_failed',
          severity: 'critical',
          message: 'Engine stopped after repeated failures',
          actions: [{ id: 'restart', label: 'Restart engine', method: 'POST', path: '/api/admin/engine/restart' }],
        },
        { id: 'queue_full', severity: 'warn', message: 'Queue full (32)', actions: [] },
      ],
    });
    await page.goto('/admin/status');
    const items = page.locator('.alertband-item');
    await expect(items).toHaveCount(3);
    expect(await items.evaluateAll((els) => els.map((e) => e.getAttribute('data-severity')))).toEqual(['critical', 'warn', 'info']);
    await expect(items.nth(0).getByText('Dismiss')).toHaveCount(0);
    await items.nth(0).getByRole('button', { name: 'Restart engine' }).click();
    await expect.poll(() => calls).toContain('POST /engine/restart');
    await items.nth(1).getByRole('button', { name: /Dismiss/ }).click();
    await expect(items).toHaveCount(2);
    expect(calls).toContain('POST /alerts/queue_full/dismiss');
  });

  test('admin key: a 401 sends the user to sign in and back', async ({ page }) => {
    await mockManager(page, { auth: { admin_requires_key: true, authenticated: false, method: null } });
    await page.goto('/admin/models/downloader');
    await expect(page).toHaveURL(/\/admin\/login\?next=%2Fmodels%2Fdownloader$/);
    await page.getByLabel('API key').fill('nope');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByText("That key didn't match.")).toBeVisible();
    await page.getByLabel('API key').fill('secret');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page).toHaveURL(/\/admin\/models\/downloader$/);
    await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
  });

  test('shell passes axe in the light theme', async ({ page }) => {
    await mockManager(page);
    await page.goto('/admin/status');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  test('shell passes axe in the dark theme with the engine stopped', async ({ page }) => {
    await mockManager(page);
    await page.addInitScript(() => localStorage.setItem('splash-gui-theme', 'dark'));
    await page.goto('/admin/status');
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    // No accent-filled text here (stopped chip, no alerts). White on the dark accent is 2.9:1,
    // so a live chip or the alert band would fail color-contrast until DESIGN.md changes.
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
