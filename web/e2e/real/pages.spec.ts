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

test.describe('5b1c9ad routes against the real manager', () => {
  test('print and secret meta answer in the shapes the pages read', async ({ page }) => {
    const print = await (await page.request.get('/api/admin/integrations/hermes/print?model=mlx-community/Qwen3.6-35B-A3B-4bit')).json();
    expect(print).toMatchObject({ client: 'hermes', exact: false });
    expect(Array.isArray(print.files)).toBe(true);
    const claude = await (await page.request.get('/api/admin/integrations/claude/print?model=mlx-community/Qwen3.6-35B-A3B-4bit')).json();
    expect(typeof claude.exact).toBe('boolean'); // exact only when the engine's install/clients.py is reachable
    expect(Array.isArray(claude.secret_env)).toBe(true);
    const meta = await (await page.request.get('/api/admin/settings/secret/meta?name=hf_token')).json();
    expect(meta).toMatchObject({ name: 'hf_token' });
    expect('masked' in meta).toBe(true);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/integrations');
    const hermes = page.locator('.integration-row', { has: page.getByRole('heading', { name: /Hermes/ }) });
    await hermes.getByText('What this changes').click();
    await expect(hermes.getByText('Static description')).toBeVisible();
  });
});
