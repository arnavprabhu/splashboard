/** Downloads panel layout: the parallel-downloads Select keeps room for its arrow, rows have no list bullets. */
import { expect, test } from '@playwright/test';

import { mockManager } from './fixtures';

const RUNNING = {
  id: 'dl-1',
  model: 'mlx-community/Qwen3.8-27B-4bit',
  language_only: false,
  verify: false,
  state: 'running',
  created_at: '2026-10-04T12:00:00Z',
  bytes_done: 268_000_000,
  bytes_total: 19_900_000_000,
  speed_bps: 18_500_000,
  eta_s: 1060,
  files: [],
  log_tail: [],
};

test('parallel downloads Select is not overlapped by its arrow; the list has no bullets', async ({ page }) => {
  await mockManager(page, { extra: (method, path) => (method === 'GET' && path === '/downloads' ? { json: { items: [RUNNING] } } : undefined) });
  await page.goto('/admin/models/downloader');
  const select = page.locator('#dl-parallel');
  await expect(select).toBeVisible();
  const paddingRight = await select.evaluate((el) => parseFloat(getComputedStyle(el).paddingRight));
  expect(paddingRight).toBeGreaterThanOrEqual(30);
  const list = page.locator('.dl-list');
  await expect(list).toHaveCSS('list-style-type', 'none');
  await expect(list).toHaveCSS('padding-left', '0px');
});

test('the downloader page has one #downloads target', async ({ page }) => {
  await mockManager(page, { extra: (method, path) => (method === 'GET' && path === '/downloads' ? { json: { items: [RUNNING] } } : undefined) });
  await page.goto('/admin/models/downloader');
  await expect(page.getByTestId('downloads')).toBeVisible();
  await expect(page.locator('[id="downloads"]')).toHaveCount(1);
});

test('the queue stays at the bottom of the viewport while downloads are active', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 520 });
  await mockManager(page, { extra: (method, path) => (method === 'GET' && path === '/downloads' ? { json: { items: [RUNNING] } } : undefined) });
  await page.goto('/admin/models/downloader');
  const panel = page.getByTestId('downloads');
  await expect(panel).toHaveAttribute('data-sticky', 'true');
  await expect(panel).toHaveCSS('position', 'sticky');
  // The page must be taller than the viewport, or the check proves nothing.
  expect(await page.evaluate(() => document.documentElement.scrollHeight > window.innerHeight)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  const box = await panel.boundingBox();
  const viewport = await page.evaluate(() => window.innerHeight);
  expect(box).not.toBeNull();
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport + 1);
});
