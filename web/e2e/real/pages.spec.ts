import { expect, test } from '../support/manager';

test.describe('pages against the real manager', () => {
  test('usage history renders the empty state from a fresh usage.db', async ({ page }) => {
    await page.goto('/admin/status/history');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(/Usage history\./i);
    await expect(page.getByText('No requests recorded.')).toBeVisible();
    const href = await page.getByRole('link', { name: 'Export CSV' }).getAttribute('href');
    expect(href).toMatch(/^\/api\/admin\/usage\/export\.csv\?start=/);
    const res = await page.request.get(href!);
    expect(res.status()).toBe(200);
    expect(await res.text()).toMatch(/^id,ts,model/);
  });

  test('settings renders the schema-driven fields and the About versions', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/settings');
    await expect(page).toHaveURL(/\/admin\/settings\/server$/);
    await expect(page.locator('[data-key="server.port"]')).toBeVisible();
    await expect(page.locator('[data-key="server.port"] .flag')).toHaveText('--port');
    await page.goto('/admin/settings/about');
    await expect(page.getByText(/manager .* · Python /)).toBeVisible();
    await expect(page.getByRole('link', { name: 'Re-run welcome wizard' })).toBeVisible();
  });

  test('integrations lists the five CLI agents and two desktop apps', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/integrations');
    for (const name of ['Claude Code', 'Codex', 'OpenCode', 'Hermes', 'Pi']) {
      await expect(page.getByRole('heading', { name: new RegExp(name) }).first()).toBeVisible();
    }
    await expect(page.getByText('$ splash launch claude').first()).toBeVisible();
    await expect(page.getByRole('tab')).toHaveCount(7);
  });

  test('chat shows the empty conversation and the panel validates', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto('/admin/chat');
    await expect(page.getByText('Say something.')).toBeVisible();
    await page.getByLabel('Top P').fill('0');
    await expect(page.getByText('Top P must be between 0 and 1.')).toBeVisible();
  });
});
