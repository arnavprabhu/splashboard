/**
 * UI QA pass 2026-10-04, batch 4 (reviewer findings): mono IDs on desktop wrap only when they must.
 */
import { expect, test } from '@playwright/test';

import { CHAT, USAGE, V1_MODELS, mockManager } from './fixtures';

const GB = 1_000_000_000;
const LONG = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL';
const DISCOVERY = { found: true, version: '1.2.0', support: 'supported' };
const MODEL = { id: LONG, repo_id: 'unsloth/Qwen3.6-35B-A3B-GGUF', variant: 'UD-Q2_K_XL', family: 'Qwen3.6-35B-A3B', format: 'gguf', language_only: false, size_bytes: 13.3 * GB, unique_bytes: 13.3 * GB, pinned: false, legacy: false, status: 'ready', last_used_at: '2026-10-04T08:00:00Z', commit: '4c0d1e2a9b' };
const DISK = { models_dir: '/Users/arnav/.splash/models', cache_dir: '/Users/arnav/.splash/cache', models_bytes: 13.3 * GB, cache_bytes: 0, free_bytes: 300 * GB, total_bytes: 1000 * GB };

test.describe('desktop, long IDs', () => {
  test.use({ viewport: { width: 1280, height: 900 } });

  test('mono IDs and paths are not broken into fragments when there is room', async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'desktop width');
    await mockManager(page, {
      engine: { state: 'ready', model: LONG, engine: DISCOVERY },
      usage: USAGE,
      extra: (_m, path) => {
        if (path === '/models') return { json: { models: [MODEL], disk: DISK } };
        if (path === '/downloads') return { json: { items: [] } };
        if (path === '/catalog') return { json: { memory_bytes: 1, families: [] } };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    for (const path of ['/admin/models', '/admin/integrations', '/admin/status/history', '/admin/tools/playground']) {
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      // An ID may wrap after "/" or ":", but never be squeezed narrower than its longest segment
      // (what `overflow-wrap: anywhere` allowed: "mlx-community/Qwen…" one or two characters per line).
      const broken = await page.evaluate(() => {
        const probe = document.createElement('span');
        probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap';
        document.body.append(probe);
        const out = [...document.querySelectorAll<HTMLElement>('.mono')]
          .filter((el) => el.offsetParent && /^[\w./:~@-]{12,80}$/.test(el.textContent?.trim() ?? ''))
          .filter((el) => {
            const cs = getComputedStyle(el);
            probe.style.font = cs.font;
            probe.style.letterSpacing = cs.letterSpacing;
            const longest = Math.max(...(el.textContent ?? '').trim().split(/(?<=[/:])/).map((seg) => ((probe.textContent = seg), probe.getBoundingClientRect().width)));
            return el.getBoundingClientRect().width < longest - 1;
          })
          .map((el) => el.textContent);
        probe.remove();
        return out;
      });
      expect(broken, `on ${path}`).toEqual([]);
    }
  });
});

test('chat: a banner added above the layout later still leaves the thread as the only scroller', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'desktop layout');
  await page.setViewportSize({ width: 1280, height: 860 });
  await mockManager(page, {
    engine: { state: 'ready', model: CHAT.model, engine: DISCOVERY },
    extra: (method, path) => {
      if (path === '/chats' && method === 'GET') return { json: { chats: [] } };
      if (path === '/chats/c1') return { json: CHAT };
      if (path.endsWith('/profiles')) return { json: { model: CHAT.model, profiles: [], sampling_defaults: {} } };
      return undefined;
    },
  });
  await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
  await page.goto('/admin/chat/c1');
  await expect(page.getByText('Here are the action items.')).toBeVisible();
  // Something above the layout grows after mount (an alert band, a banner): no window resize happens.
  await page.evaluate(() => {
    const banner = document.createElement('div');
    banner.style.height = '120px';
    document.querySelector('main')!.prepend(banner);
  });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollHeight - innerHeight)).toBeLessThanOrEqual(1);
  await expect(page.getByRole('textbox', { name: 'Message' })).toBeInViewport();
});

test('Downloader: a picked variant shows its own fit, not the default variant’s memory need', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'desktop layout');
  const repo = 'unsloth/Qwen3.8-27B-GGUF';
  const entry = {
    id: repo, repo_id: repo, family: 'Qwen3.8-27B', format: 'gguf', size_bytes: 16.5 * GB, memory_need_bytes: 20 * GB, fit: 'fits', installed: false, recommended: false,
    recommended_variant: 'UD-Q4_K_M',
    variants: [
      { name: 'UD-Q4_K_M', size_bytes: 16.5 * GB, fit: 'fits', loadable: null, recommended: true },
      { name: 'BF16', size_bytes: 54 * GB, fit: 'wont_fit', loadable: null, recommended: false },
    ],
  };
  await mockManager(page, {
    engine: { state: 'stopped', model: null, engine: DISCOVERY },
    extra: (_m, path) => {
      if (path === '/catalog') return { json: { memory_bytes: 32 * 1024 ** 3, families: [{ family: 'Qwen3.8-27B', label: 'Qwen3.8-27B', groups: [{ format: 'gguf', entries: [entry] }] }] } };
      if (path === '/models') return { json: { models: [], disk: DISK } };
      if (path === '/downloads') return { json: { items: [] } };
      return undefined;
    },
  });
  await page.goto('/admin/models/downloader?tab=supported');
  const row = page.locator('.dlr-entry').first();
  await row.getByRole('button', { name: 'Variant' }).click();
  await page.getByRole('menuitemradio', { name: /BF16/ }).click();
  const tag = row.locator('.dlr-entry-tags').getByText('Won’t fit');
  await expect(tag).toBeVisible();
  // The entry's need (20 GB = "18.6 GB") belongs to UD-Q4_K_M; quoting it for BF16 would be wrong.
  expect(await tag.getAttribute('title')).toBeNull();
});
