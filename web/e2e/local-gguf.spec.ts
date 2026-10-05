/**
 * Local GGUF drop-in on Models → Manager (SPEC §9.6, D49), against stubbed admin routes:
 * the Local tag, the drop note with the real models folder, Rescan folder and the error lines.
 * `SPLASH_GUI_SHOT=<path>` also saves a desktop screenshot (docs/progress/ui-qa-shots/).
 */
import { expect, test } from '@playwright/test';

import { mockManager } from './fixtures';

declare const process: { env: Record<string, string | undefined> };

const GB = 1_000_000_000;
const DIR = '/Users/arnav/.splash/models';
const LOCAL = 'local/Qwen3.6-35B-A3B-UD-Q4_K_XL-GGUF';
const HUB = 'mlx-community/Qwen3.8-27B-4bit';
const DISK = { models_dir: DIR, cache_dir: '/Users/arnav/.splash/cache', models_bytes: 37.4 * GB, cache_bytes: 2.1 * GB, free_bytes: 300 * GB, total_bytes: 1000 * GB };
const row = (id: string, extra: Record<string, unknown>) => ({ id, repo_id: id, language_only: false, unique_bytes: 0, pinned: false, legacy: false, status: 'ready', last_used_at: null, ...extra });
const MODELS = [
  row(LOCAL, { family: 'Qwen3.6-35B-A3B', format: 'gguf', language_only: true, size_bytes: 21.7 * GB, unique_bytes: 21.7 * GB, commit: '9f2c4e1a7b' }),
  row(HUB, { family: 'Qwen3.8-27B', format: 'mlx', size_bytes: 15.7 * GB, unique_bytes: 15.7 * GB, commit: '4c0d1e2a9b', last_used_at: '2026-10-04T08:00:00Z' }),
];
const FAILED = { 'llama-3-8b-instruct.Q4_K_M.gguf': { error: 'unsupported GGUF model architecture: llama' } };

test('Models: Local tag, drop note, Rescan folder and a line per file not added', async ({ page }, info) => {
  const posts: string[] = [];
  await mockManager(page, {
    extra: (method, path, url) => {
      if (path === '/models') return { json: { models: MODELS, disk: DISK } };
      if (path === '/downloads') return { json: { items: [] } };
      if (path === '/models/local') return { json: { files: { 'Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf': { model: LOCAL }, ...FAILED }, ignored: [] } };
      if (path === '/models/local/rescan' && method === 'POST') return posts.push(url.search), { json: { added: [], files: FAILED, ignored: [] } };
      return undefined;
    },
  });
  await page.goto('/admin/models');

  const local = page.getByTestId('model-row').filter({ hasText: LOCAL });
  await expect(local.locator('.tag', { hasText: /^Local$/ })).toHaveCount(1);
  await expect(page.getByTestId('model-row').filter({ hasText: HUB }).locator('.tag', { hasText: /^Local$/ })).toHaveCount(0);

  const drop = page.getByTestId('local-drop');
  await expect(drop).toContainText(`Drop a .gguf into ${DIR} and it is added automatically.`);
  await expect(page.getByTestId('local-errors').getByRole('listitem')).toHaveText(['llama-3-8b-instruct.Q4_K_M.ggufunsupported GGUF model architecture: llama']);

  if (process.env.SPLASH_GUI_SHOT && info.project.name === 'chromium') {
    await page.setViewportSize({ width: 1280, height: 1400 });
    await page.waitForLoadState('networkidle');
    await page.screenshot({ path: process.env.SPLASH_GUI_SHOT, fullPage: true, type: 'jpeg', quality: 80 });
  }

  await page.getByRole('button', { name: 'Rescan folder' }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toBe('');
  await expect(page.getByText('No new .gguf files.')).toBeVisible();

  // The drawer tags it too and does not link to a Hub repo that does not exist.
  await local.getByRole('button', { name: `Open details for ${LOCAL}` }).click();
  const drawer = page.getByTestId('model-drawer');
  await expect(drawer.locator('.tag', { hasText: /^Local$/ })).toHaveCount(1);
  await expect(drawer.getByText('View on Hugging Face')).toHaveCount(0);
});
