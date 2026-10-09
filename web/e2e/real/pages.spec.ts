import { readFileSync } from 'node:fs';
import { join } from 'node:path';

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

  test('memory and context: Auto toggles are named after the field, one size line (acceptance 2026-10-04)', async ({ page, manager }) => {
    await manager.patchSettings((doc) => {
      doc.global.serve = { ...doc.global.serve, max_memory: '40G', max_context: '128K' };
    });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/settings/memory');
    const memory = page.locator('[data-key="serve.max_memory"]');
    await expect(memory.getByRole('switch', { name: 'Auto (Memory ceiling)' })).toBeVisible();
    await expect(page.locator('[data-key="serve.max_context"]').getByRole('switch', { name: 'Auto (Context limit)' })).toBeVisible();
    await expect(page.getByRole('switch', { name: /\{value\}/ })).toHaveCount(0);
    // The parsed size was printed twice: once by SizeInput, once by the memory footnote.
    await expect(memory.getByText(/^= 40 GB/)).toHaveCount(1);
    // ...and that one line carries the share of this Mac's RAM.
    await expect(memory.getByText(/^= 40 GB · \d+% of \d+ GB$/)).toBeVisible();
  });

  test('saving one global field adds no per-model keys (acceptance 2026-10-04)', async ({ page, manager }) => {
    const model = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL';
    await manager.patchSettings((doc) => {
      doc.global.serve = { ...doc.global.serve, max_context: '128K' };
      doc.models[model] = { serve: { served_model_names: ['qwen-moe'] } };
    });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/settings/requests');
    await page.locator('[data-key="serve.queue_size"] input').fill('16');
    await page.getByRole('region', { name: 'Unsaved changes' }).getByRole('button', { name: /^Save/ }).click();
    await expect(page.getByText('Settings saved.')).toBeVisible();
    const disk = JSON.parse(readFileSync(join(manager.home, 'settings.json'), 'utf8'));
    expect(disk.global.serve.queue_size).toBe(16);
    expect(disk.models[model]).toEqual({ serve: { served_model_names: ['qwen-moe'] }, sampling_defaults: {}, profiles: {} });
    const preview = await manager.api<{ argv: string[] }>('GET', `/settings/launch-preview?model=${encodeURIComponent(model)}`);
    expect(preview.body.argv.join(' ')).toContain('--max-context 128K');
  });

  test('performance: GPU-only prefill maps to --disable-ane (Splash 1.3.0)', async ({ page, manager }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/settings/performance');
    const field = page.locator('[data-key="serve.disable_ane"]');
    await expect(field.locator('.flag')).toHaveText('--disable-ane');
    await expect(field.getByText('GPU-only prefill')).toBeVisible();
    await expect(field.getByText(/the 35B MoE always prefills on the GPU/)).toBeVisible();
    await expect(page.locator('[data-key="serve.allow_idle_sleep"] .flag')).toHaveText('--allow-idle-sleep');
    await field.getByRole('switch').click();
    await page.getByRole('region', { name: 'Unsaved changes' }).getByRole('button', { name: /^Save/ }).click();
    await expect(page.getByText('Settings saved.')).toBeVisible();
    const disk = JSON.parse(readFileSync(join(manager.home, 'settings.json'), 'utf8'));
    expect(disk.global.serve.disable_ane).toBe(true);
    const preview = await manager.api<{ argv: string[] }>('GET', `/settings/launch-preview?model=${encodeURIComponent('unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL')}`);
    expect(preview.body.argv).toContain('--disable-ane');
  });

  test('memory: an invalid idle release shows Splash’s message', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/settings/memory');
    const field = page.locator('[data-key="serve.idle_release"]');
    await expect(field.locator('.flag')).toHaveText('--idle-release');
    await field.locator('input').fill('never');
    await expect(field.getByText('must be off or a positive duration such as 30m, 2h or 600')).toBeVisible();
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
  test('print and secret meta answer in the shapes the pages read', async ({ page, manager }) => {
    // /print runs programs, so it is a read-only POST from a same-origin page.
    const sameOrigin = { headers: { Origin: manager.url, 'Sec-Fetch-Site': 'same-origin' } };
    expect((await page.request.get('/api/admin/integrations/hermes/print')).status()).toBe(405);
    const print = await (await page.request.post('/api/admin/integrations/hermes/print?model=mlx-community/Qwen3.6-35B-A3B-4bit', sameOrigin)).json();
    expect(print).toMatchObject({ client: 'hermes', exact: false });
    expect(Array.isArray(print.files)).toBe(true);
    const claude = await (await page.request.post('/api/admin/integrations/claude/print?model=mlx-community/Qwen3.6-35B-A3B-4bit', sameOrigin)).json();
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
