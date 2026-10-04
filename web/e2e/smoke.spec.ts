import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

import { mockManager, USAGE } from './fixtures';

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
  '/admin/settings',
  '/admin/settings/cache',
  '/admin/settings/about',
  '/admin/chat/c1',
  '/admin/does-not-exist',
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
      // `overflow-x: hidden` on body is a safety net, not a strategy
      // (docs/ui/00-foundations.md §2.3): neutralise it so a regression cannot
      // hide behind it, then measure what the layout really wants.
      const overflow = await page.evaluate(() => {
        const style = document.createElement('style');
        style.textContent = 'html,body{overflow-x:visible !important}';
        document.head.append(style);
        return document.body.scrollWidth - document.documentElement.clientWidth;
      });
      expect(overflow, `on ${path}`).toBeLessThanOrEqual(0);
    }
  });

  test('no horizontal scroll with populated data', async ({ page }) => {
    // An empty heatmap and an empty request-log table never exercise the widest
    // boxes, so the sweep above cannot see them.
    await mockManager(page, { usage: USAGE });
    await page.setViewportSize({ width: 360, height: 780 });
    await page.goto('/admin/status/history');
    await expect(page.locator('.heat-cell').first()).toBeVisible();
    await expect(page.locator('.history-log tbody tr')).toHaveCount(50);
    const overflow = await page.evaluate(() => {
      const style = document.createElement('style');
      style.textContent = 'html,body{overflow-x:visible !important}';
      document.head.append(style);
      return document.body.scrollWidth - document.documentElement.clientWidth;
    });
    expect(overflow).toBeLessThanOrEqual(0);

    // Wide content must scroll inside its own container, never the page.
    const contained = await page.evaluate(() => {
      const scrollable = ['.table-scroll', '.heatmap'].map((sel) => {
        const el = document.querySelector<HTMLElement>(sel);
        if (!el) return { sel, missing: true };
        return { sel, clientWidth: el.clientWidth, scrollWidth: el.scrollWidth };
      });
      return scrollable;
    });
    for (const box of contained) {
      expect(box, JSON.stringify(box)).not.toHaveProperty('missing');
      expect((box as { clientWidth: number }).clientWidth).toBeLessThanOrEqual(360);
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
    await page.getByLabel('Admin key', { exact: true }).fill('nope');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByText('That key didn’t match.')).toBeVisible();
    await page.getByLabel('Admin key', { exact: true }).fill('secret');
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
