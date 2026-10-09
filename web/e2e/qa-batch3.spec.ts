/**
 * UI QA pass 2026-10-04, batch 3: the drawer's variant menu selects (like the catalog's), and a
 * finished download shows its size and no Load once the model is gone.
 */
import { expect, test } from '@playwright/test';

import { mockManager } from './fixtures';

const GB = 1_000_000_000;
const GGUF = 'unsloth/Qwen3.6-35B-A3B-GGUF';
const READY = { state: 'ready', model: `${GGUF}:UD-Q2_K_XL`, engine: { found: true, version: '1.2.0', support: 'supported' } };
const DISK = { models_dir: '/m', cache_dir: '/c', models_bytes: 0, cache_bytes: 0, free_bytes: 300 * GB, total_bytes: 1000 * GB };

test('drawer: choosing a variant selects it; only the Download button downloads', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'desktop layout');
  const posts: unknown[] = [];
  await mockManager(page, {
    engine: READY,
    extra: (method, path, _url, body) => {
      if (path === '/inspect')
        return {
          json: {
            id: GGUF,
            repo_id: GGUF,
            compatible: true,
            badge: 'compatible',
            family: 'Qwen3.6-35B-A3B',
            format: 'gguf',
            vision: { available: true },
            cached: false,
            checked_at: '',
            variants: [
              { name: 'UD-Q2_K_XL', size_bytes: 13.3 * GB, loadable: true },
              { name: 'UD-Q4_K_M', size_bytes: 21 * GB, download_bytes: 22 * GB, loadable: true, recommended: true },
            ],
          },
        };
      if (path === '/card') return { json: { markdown: '', files: [], tags: [] } };
      if (path === '/catalog') return { json: { memory_bytes: 1, families: [] } };
      if (path === '/models') return { json: { models: [], disk: DISK } };
      if (path === '/downloads' && method === 'POST') return posts.push(body), { json: { id: 'd1', model: 'x', state: 'queued', created_at: '2026-10-04T09:00:00Z', bytes_done: 0, language_only: false, verify: false } };
      if (path === '/downloads') return { json: { items: [] } };
      return undefined;
    },
  });
  await page.goto(`/admin/models/downloader?tab=supported&model=${encodeURIComponent(GGUF)}`);
  const drawer = page.getByTestId('model-drawer');
  const button = drawer.getByTestId('drawer-download');
  await expect(button).toHaveText('Download UD-Q4_K_M · 22 GB');
  await drawer.getByRole('button', { name: 'Variant' }).click();
  await page.getByRole('menuitemradio', { name: /UD-Q2_K_XL/ }).click();
  await expect(button).toHaveText('Download UD-Q2_K_XL · 13.3 GB');
  expect(posts).toEqual([]);
  await button.click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toMatchObject({ id: `${GGUF}:UD-Q2_K_XL` });
});

test('row 25: a finished download shows its size, and no Load once the model was deleted', async ({ page }) => {
  const done = { id: 'd1', model: 'mlx-community/Qwen3.8-27B-4bit', language_only: false, verify: false, state: 'done', created_at: '2026-10-04T09:00:00Z', finished_at: '2026-10-04T09:30:00Z', bytes_done: null, bytes_total: 21.5 * GB };
  await mockManager(page, {
    engine: READY,
    extra: (_m, path) => {
      if (path === '/catalog') return { json: { memory_bytes: 1, families: [] } };
      if (path === '/models') return { json: { models: [], disk: DISK } };
      if (path === '/downloads') return { json: { items: [done] } };
      return undefined;
    },
  });
  await page.goto('/admin/models/downloader?tab=supported');
  const panel = page.locator('#downloads');
  await panel.getByRole('button', { name: /Show completed/ }).click().catch(() => undefined);
  await expect(panel).toContainText('21.5 GB');
  await expect(panel).not.toContainText('0 B of');
  await expect(panel.getByTestId('dl-removed')).toHaveText('Removed from this Mac');
  await expect(panel.getByRole('button', { name: 'Load', exact: true })).toHaveCount(0);
});
