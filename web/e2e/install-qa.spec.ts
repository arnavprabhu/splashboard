/**
 * UI QA pass 2026-10-04 (known bugs 1–4), against stubbed admin routes:
 * the wizard's Download button after a cancel, the tier sentence, download sizes, and the engine
 * install progress plus the confirmation before interrupting it.
 */
import { expect, test, type Page } from '@playwright/test';

import { mockManager } from './fixtures';

const MLX_27B = 'mlx-community/Qwen3.8-27B-4bit';
const MOE = 'mlx-community/Qwen3.6-35B-A3B-4bit';
const GB = 1_000_000_000;
const DISCOVERY = { found: true, version: '1.2.0', support: 'supported' };
const INSTALL = { repo: MLX_27B, revision: '4c0d1e2a9b', files: 9, total_bytes: 19.93 * GB, done_bytes: 8.6 * GB, speed_bps: 84_000_000, eta_s: 135 };
const INSTALLING = { state: 'starting', phase: 'installing', model: MLX_27B, since: '2026-10-04T09:00:00Z', install: INSTALL, log_tail: ['Fetching 9 file(s), 19.93 GB, from mlx-community/Qwen3.8-27B-4bit@4c0d1e2'], engine: DISCOVERY };

const PRESETS = {
  memory_bytes: 68_719_476_736,
  presets: [
    { id: 'chat', label: 'Chat & general', description: 'd', settings: {}, recommendation: { primary: { model: MLX_27B, note: 'Best quality' }, alternatives: [], reason: '≥ 48 GB' } },
  ],
};
const CATALOG = {
  memory_bytes: 68_719_476_736,
  offline: false,
  families: [
    {
      family: 'Qwen3.8-27B',
      label: 'Qwen3.8-27B',
      groups: [
        {
          format: 'mlx',
          entries: [{ id: MLX_27B, repo_id: MLX_27B, family: 'Qwen3.8-27B', format: 'mlx', size_bytes: 15 * GB, download_bytes: 19.93 * GB, fit: 'fits', installed: false, recommended: true }],
        },
      ],
    },
  ],
};
const download = (state: string) => ({ id: 'd1', model: MLX_27B, language_only: false, verify: false, state, created_at: '2026-10-04T09:00:00Z', bytes_done: 2 * GB, bytes_total: 19.93 * GB });

async function wizardStep4(page: Page, state: string, posts: unknown[] = []) {
  await page.addInitScript((m) => localStorage.setItem('splash-gui-wizard', JSON.stringify({ step: 4, reached: 4, pendingPort: null, preset: 'chat', model: m, downloadId: 'd1' })), MLX_27B);
  await mockManager(page, {
    extra: (method, path, _url, body) => {
      if (path === '/downloads' && method === 'POST') return posts.push(body), { json: { ...download('queued'), id: 'd2' } };
      if (path === '/downloads') return { json: { items: [download(state)] } };
      if (path === '/settings/presets') return { json: PRESETS };
      if (path === '/catalog') return { json: CATALOG };
      if (path === '/models') return { json: { models: [], disk: { models_dir: '/m', cache_dir: '/c', models_bytes: 0, cache_bytes: 0, free_bytes: 300 * GB, total_bytes: 1000 * GB } } };
      return undefined;
    },
  });
  await page.goto('/admin/welcome?step=4');
}

test.describe('wizard step 4', () => {
  test('bug 1: after a cancel the Download button works again and starts a fresh download', async ({ page }) => {
    const posts: unknown[] = [];
    await wizardStep4(page, 'cancelled', posts);
    // Not by its size label: on the old code the cancelled download's ID kept this disabled.
    const button = page.locator('.wz-model').getByRole('button', { name: /^Download/ });
    await expect(button).toBeEnabled();
    await button.click();
    await expect.poll(() => posts.length).toBe(1);
    expect(posts[0]).toMatchObject({ id: MLX_27B });
  });

  test('bug 1: while the download runs the button is disabled and says why', async ({ page }) => {
    await wizardStep4(page, 'running');
    await expect(page.getByRole('button', { name: 'Downloading — see below' })).toBeDisabled();
  });

  test('bug 2 and 3: the tier is a sentence; the card shows what the download fetches', async ({ page }) => {
    await wizardStep4(page, 'cancelled');
    await expect(page.getByText('Picked for Macs with 48 GB of memory or more.')).toBeVisible();
    await expect(page.getByText('≥ 48 GB', { exact: true })).toHaveCount(0);
    await expect(page.locator('.wz-model-head .label')).toHaveText('MLX · 19.9 GB');
    await expect(page.getByText(/15 GB/)).toHaveCount(0);
  });
});

test('bug 3: the Downloader catalog sizes rows by download_bytes, and labels bare weights', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'desktop layout');
  const weightsOnly = { ...CATALOG.families[0]!.groups[0]!.entries[0]!, id: MOE, repo_id: MOE, download_bytes: null, size_bytes: 21 * GB, recommended: false };
  const catalog = { ...CATALOG, families: [{ ...CATALOG.families[0], groups: [{ format: 'mlx', entries: [CATALOG.families[0]!.groups[0]!.entries[0], weightsOnly] }] }] };
  await mockManager(page, {
    extra: (_method, path) => {
      if (path === '/catalog') return { json: catalog };
      if (path === '/downloads') return { json: { items: [] } };
      if (path === '/models') return { json: { models: [], disk: { models_dir: '/m', cache_dir: '/c', models_bytes: 0, cache_bytes: 0, free_bytes: 300 * GB, total_bytes: 1000 * GB } } };
      return undefined;
    },
  });
  await page.goto('/admin/models/downloader?tab=supported');
  const first = page.locator('.dlr-entry').nth(0);
  await expect(first.locator('.dlr-entry-tags .label')).toHaveText('01 — MLX 4-bit · 19.9 GB');
  await expect(first.getByRole('button', { name: 'Download · 19.9 GB' })).toBeVisible();
  const second = page.locator('.dlr-entry').nth(1);
  await expect(second.locator('.dlr-entry-tags .label')).toHaveText('02 — MLX 4-bit · weights 21 GB');
  await expect(second.getByRole('button', { name: 'Download', exact: true })).toBeVisible();
});

test.describe('bug 4: engine install', () => {
  test('Status shows repo, files, bytes, speed, ETA and the warning while Splash installs', async ({ page }) => {
    await mockManager(page, { engine: INSTALLING });
    await page.goto('/admin/status');
    const band = page.getByTestId('engine-install');
    await expect(band).toContainText(`${MLX_27B}@4c0d1e2`);
    await expect(band).toContainText('9 files');
    await expect(band).toContainText('8.6 GB of 19.9 GB · 84 MB/s · 2 m 15 s left');
    await expect(band).toContainText('Stopping now restarts the file in progress from zero');
    await expect(band.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '43');
  });

  test('the wizard load step shows the install too', async ({ page }) => {
    await page.addInitScript((m) => localStorage.setItem('splash-gui-wizard', JSON.stringify({ step: 5, reached: 5, pendingPort: null, preset: 'chat', model: m, downloadId: null })), MLX_27B);
    await mockManager(page, {
      engine: INSTALLING,
      extra: (_method, path) => {
        if (path === '/downloads') return { json: { items: [] } };
        if (path === '/models') return { json: { models: [{ id: MLX_27B, repo_id: MLX_27B, format: 'mlx', language_only: false, size_bytes: 1, unique_bytes: 1, pinned: false, legacy: false, status: 'loading' }], disk: {} } };
        return undefined;
      },
    });
    await page.goto('/admin/welcome?step=5');
    await expect(page.getByTestId('engine-install')).toContainText('8.6 GB of 19.9 GB');
  });

  for (const known of [true, false]) {
    test(`Load of another model ${known ? 'while the page shows the install' : 'on a 409 install_in_progress'} asks, then sends force`, async ({ page }, info) => {
      test.skip(info.project.name === 'phone', 'desktop layout');
      const loads: Array<Record<string, unknown>> = [];
      const ready = { state: 'ready', model: MLX_27B, since: '2026-10-04T09:00:00Z', engine: DISCOVERY };
      const installed = { ...CATALOG.families[0]!.groups[0]!.entries[0]!, id: MOE, repo_id: MOE, installed: true };
      const catalog = { ...CATALOG, families: [{ ...CATALOG.families[0], groups: [{ format: 'mlx', entries: [installed] }] }] };
      await mockManager(page, {
        engine: known ? INSTALLING : ready,
        extra: (method, path, _url, body) => {
          if (path === '/engine/load' && method === 'POST') {
            const b = body as Record<string, unknown>;
            loads.push(b);
            if (!b.force) return { status: 409, json: { error: { message: 'Splash is downloading model files', type: 'conflict_error', code: 'install_in_progress' } } };
            return { json: { ...ready, state: 'starting', phase: 'loading', model: MOE } };
          }
          if (path === '/catalog') return { json: catalog };
          if (path === '/downloads') return { json: { items: [] } };
          if (path === '/models') return { json: { models: [], disk: { models_dir: '/m', cache_dir: '/c', models_bytes: 0, cache_bytes: 0, free_bytes: 300 * GB, total_bytes: 1000 * GB } } };
          return undefined;
        },
      });
      await page.goto('/admin/models/downloader?tab=supported');
      await page.locator('.dlr-entry').getByRole('button', { name: 'Load' }).click();
      const sheet = page.getByRole('alertdialog', { name: 'Interrupt the download.' });
      await expect(sheet).toContainText('the file in progress starts again from zero');
      // No keeps the install going and sends nothing more.
      await sheet.getByRole('button', { name: 'Cancel' }).click();
      await expect(sheet).toBeHidden();
      expect(loads.filter((b) => b.force)).toEqual([]);
      await page.locator('.dlr-entry').getByRole('button', { name: 'Load' }).click();
      await page.getByRole('alertdialog').getByRole('button', { name: 'Interrupt' }).click();
      await expect.poll(() => loads.filter((b) => b.force).length).toBe(1);
      expect(loads.at(-1)).toMatchObject({ model: MOE, force: true });
    });
  }
});
