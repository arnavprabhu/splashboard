/**
 * Tools > Playground: Send through the manager, raw mode, copy-as and history.
 * Shell smoke tests: the manager is stubbed with page.route (fixtures.ts), so these prove the
 * page's requests and state, not the engine's answers. The raw path is proven on the manager
 * side by manager/tests/test_proxy.py.
 */
import { expect, test } from '@playwright/test';
import { mockManager } from './fixtures';

const MODEL = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL';
const READY = {
  state: 'ready',
  model: MODEL,
  since: '2026-10-04T09:00:00Z',
  engine: { found: true, version: '1.3.0', support: 'supported', supported_range: '>=1.3.0,<1.4.0', source_checkout: false },
  requests_in_flight: 0,
  maximum_context_tokens: 262144,
};

// The page's default chat template streams, so the stub answers with server-sent events.
const STREAM = [
  'data: {"choices":[{"delta":{"content":"Hello"}}]}',
  '',
  'data: {"choices":[{"delta":{"content":" from the stub"}}]}',
  '',
  'data: [DONE]',
  '',
  '',
].join('\n');

test('Send goes through the profiles path, shows the stream and records one history row', async ({ page }) => {
  await mockManager(page, { engine: READY });
  const seen: string[] = [];
  await page.route('**/v1/chat/completions', (r) => {
    seen.push(r.request().url());
    return r.fulfill({ status: 200, contentType: 'text/event-stream', body: STREAM });
  });
  await page.goto('/admin/tools/playground');
  await page.getByRole('button', { name: 'Send', exact: true }).click();

  await expect(page.getByText('[DONE]').first()).toBeVisible();
  expect(seen).toHaveLength(1);
  expect(new URL(seen[0]!).pathname).toBe('/v1/chat/completions');
  await expect(page.locator('.pg-history-row')).toHaveCount(1);
  const stored = await page.evaluate(() => localStorage.getItem('playground.history'));
  const rows = JSON.parse(stored ?? '[]') as Array<{ path: string; mode: string; status: number | null }>;
  expect(rows).toHaveLength(1);
  expect(rows[0]).toMatchObject({ path: '/v1/chat/completions', mode: 'profiles', status: 200 });
});

test('Raw to engine sends the request to /api/admin/engine/raw and is labelled Raw', async ({ page }) => {
  await mockManager(page, { engine: READY });
  const seen: string[] = [];
  await page.route('**/api/admin/engine/raw/**', (r) => {
    seen.push(new URL(r.request().url()).pathname);
    return r.fulfill({ status: 200, contentType: 'text/event-stream', body: STREAM });
  });
  await page.goto('/admin/tools/playground');
  await page.getByRole('radio', { name: 'Raw to engine' }).click();
  await expect(page.getByText('Raw to engine', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Send', exact: true }).click();

  await expect(page.getByText('[DONE]').first()).toBeVisible();
  expect(seen).toEqual(['/api/admin/engine/raw/v1/chat/completions']);
  await expect(page.locator('.pg-history-row')).toHaveCount(1);
  const rows = JSON.parse((await page.evaluate(() => localStorage.getItem('playground.history'))) ?? '[]') as Array<{ mode: string }>;
  expect(rows[0]?.mode).toBe('raw');
});

test('Copy as curl puts a runnable snippet on the clipboard', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await mockManager(page, { engine: READY });
  await page.goto('/admin/tools/playground');
  await page.getByRole('button', { name: 'Copy as' }).click();
  await page.getByRole('menuitem', { name: 'curl' }).click();

  await expect(page.getByText('Copied curl.')).toBeVisible();
  const text = await page.evaluate(() => navigator.clipboard.readText());
  expect(text).toContain('curl ');
  expect(text).toContain('/v1/chat/completions');
  expect(text).toContain('"stream":true');
  expect(text).not.toContain('Authorization');
});
