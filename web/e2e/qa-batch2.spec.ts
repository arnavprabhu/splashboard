/**
 * UI QA pass 2026-10-04, batch 2 (docs/progress/ui-qa-pass.md rows 1, 5, 6, 8, 9, 11, 15, 17–19,
 * 21–23), against stubbed admin routes. Each test fails on the code before the fix.
 */
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { expect, test, type Page } from '@playwright/test';

import { CHAT, SETTINGS, V1_MODELS, mockManager } from './fixtures';

const GB = 1_000_000_000;
const DISCOVERY = { found: true, version: '1.2.0', support: 'supported', supported_range: '>=1.2.0,<1.3.0', source_checkout: false };
const Q2 = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL';
const GGUF = 'unsloth/Qwen3.6-35B-A3B-GGUF';
const MLX_27B = 'mlx-community/Qwen3.8-27B-4bit';
const READY = { state: 'ready', model: Q2, since: '2026-10-04T09:00:00Z', engine: DISCOVERY, requests_in_flight: 0, maximum_context_tokens: 262144 };
const VOL = { path: '/Users/arnav/.splash/models', total_bytes: 1000 * GB, free_bytes: 300 * GB };
const SYSTEM = { chip: 'Apple M5 Pro', memory_bytes: 68_719_476_736, macos_version: '27.0', arch: 'arm64', hostname: 'mac', supported: true, unsupported_reasons: [], disk: { models: VOL, cache: VOL }, power: { source: 'ac' } };
const DISK = { models_dir: VOL.path, cache_dir: '/Users/arnav/.splash/cache', models_bytes: 13.3 * GB, cache_bytes: 2.1 * GB, free_bytes: 300 * GB, total_bytes: 1000 * GB };
const INSTALLED_Q2 = { id: Q2, repo_id: GGUF, variant: 'UD-Q2_K_XL', family: 'Qwen3.6-35B-A3B', format: 'gguf', language_only: false, size_bytes: 13.3 * GB, unique_bytes: 13.3 * GB, pinned: false, legacy: false, status: 'ready', last_used_at: '2026-10-04T08:00:00Z' };
const settingsWith = (wizard: Record<string, unknown>) => ({ ...SETTINGS, settings: { ...SETTINGS.settings, global: { ...SETTINGS.settings.global, wizard } } });

// ---------- wizard (rows 1, 5, 6) ----------

test.describe('wizard', () => {
  test('row 1: step 1 shows the doctor’s `path` warning about a splash() function, not ✓', async ({ page }) => {
    await mockManager(page, {
      extra: (_m, path) => {
        if (path === '/system') return { json: SYSTEM };
        if (path === '/system/brew') return { json: { installed: true, path: '/opt/homebrew/bin/brew', version: '5.0.0', splash_formula_installed: true } };
        if (path === '/versions') return { json: { gui: '0.1.0', manager: '0.1.0', python: '3.12', engine: { ...DISCOVERY, cli: '/opt/homebrew/opt/splash/libexec/python/bin/splash-cli-entry-point-with-a-long-name' }, engine_update: { available: false } } };
        if (path === '/doctor')
          return { json: { ok: true, checks: [{ id: 'path', label: 'CLI PATH', status: 'warn', message: 'A splash function or alias hides the shim: /Users/arnav/.zshrc', fix: 'Remove the splash function or alias' }] } };
        return undefined;
      },
    });
    await page.goto('/admin/welcome?step=1');
    const row = page.getByTestId('check-shell');
    await expect(row).toHaveAttribute('data-status', 'warn');
    await expect(row).toContainText('/Users/arnav/.zshrc');
    await expect(row).not.toContainText('No conflicting splash command found');
  });

  test('row 1: a doctor without the check shows unknown, not ✓', async ({ page }) => {
    await mockManager(page, {
      extra: (_m, path) => {
        if (path === '/system') return { json: SYSTEM };
        if (path === '/doctor') return { json: { ok: true, checks: [{ id: 'disk', label: 'Disk', status: 'ok', message: 'ok', fix: null }] } };
        return undefined;
      },
    });
    await page.goto('/admin/welcome?step=1');
    await expect(page.getByTestId('check-shell')).toHaveAttribute('data-status', 'skip');
  });

  test('row 5: a re-run starts step 3 from the saved preset (Coding), not Chat', async ({ page }) => {
    await page.addInitScript(() => localStorage.removeItem('splash-gui-wizard'));
    const presets = ['coding', 'chat', 'speed'].map((id) => ({ id, label: id === 'coding' ? 'Coding agents' : id === 'chat' ? 'Chat & general' : 'Max speed', description: 'd', settings: {}, recommendation: { primary: null, alternatives: [], reason: '' } }));
    await mockManager(page, {
      extra: (_m, path) => {
        if (path === '/settings') return { json: settingsWith({ completed: true, preset: 'coding' }) };
        if (path === '/settings/presets') return { json: { memory_bytes: SYSTEM.memory_bytes, presets } };
        if (path === '/settings/effective') return { json: { values: {} } };
        if (path === '/settings/schema') return { json: { fields: [] } };
        return undefined;
      },
    });
    await page.goto('/admin/welcome?step=3');
    await expect(page.getByRole('radio', { name: /Coding agents/ })).toBeChecked();
    await expect(page.getByRole('radio', { name: /Chat & general/ })).not.toBeChecked();
  });

  test('row 6: step 4 offers the installed model with Use, preselects it, and step 5 can load it', async ({ page }) => {
    await page.addInitScript(() => {
      if (!sessionStorage.getItem('seeded')) {
        sessionStorage.setItem('seeded', '1');
        localStorage.setItem('splash-gui-wizard', JSON.stringify({ step: 4, reached: 4, pendingPort: null, preset: 'chat', model: null, downloadId: null }));
      }
    });
    const loads: unknown[] = [];
    await mockManager(page, {
      engine: { state: 'stopped', model: null, engine: DISCOVERY },
      extra: (method, path, _url, body) => {
        if (path === '/settings/presets')
          return { json: { memory_bytes: SYSTEM.memory_bytes, presets: [{ id: 'chat', label: 'Chat & general', description: 'd', settings: {}, recommendation: { primary: { model: MLX_27B, note: 'best' }, alternatives: [], reason: '≥ 48 GB' } }] } };
        if (path === '/catalog') return { json: { memory_bytes: SYSTEM.memory_bytes, families: [] } };
        if (path === '/models') return { json: { models: [INSTALLED_Q2], disk: DISK } };
        if (path === '/downloads') return { json: { items: [] } };
        if (path === '/engine/load' && method === 'POST') return loads.push(body), { json: { state: 'starting', phase: 'loading', model: Q2, engine: DISCOVERY } };
        return undefined;
      },
    });
    await page.goto('/admin/welcome?step=4');
    const group = page.getByTestId('wz-installed');
    await expect(group).toContainText(Q2);
    await expect(group.getByRole('button', { name: 'Selected' })).toBeVisible();
    await page.getByRole('button', { name: 'Continue' }).click();
    await expect(page.getByText('No model yet.')).toHaveCount(0);
    const load = page.getByRole('button', { name: 'Load model & start server' });
    await expect(load).toBeEnabled();
    await load.click();
    await expect.poll(() => loads.length).toBe(1);
    expect(loads[0]).toMatchObject({ model: Q2 });
  });
});

// ---------- Status (rows 8, 9) ----------

function series(n = 30) {
  const now = Math.floor(Date.now() / 1000);
  const t = Array.from({ length: n }, (_, i) => now - (n - i) * 2);
  const flat = (v: number | null) => t.map(() => v);
  return {
    t,
    series: {
      'throughput.decode_tps': flat(74),
      'memory.charged_bytes': flat(55.9 * 1024 ** 3),
      'memory.current_bytes': flat(50 * 1024 ** 3),
      'memory.limit_bytes': flat(58 * 1024 ** 3),
      'cache.hit_rate': flat(null),
    },
  };
}

async function readyStatus(page: Page) {
  await mockManager(page, {
    engine: READY,
    extra: (_m, path) => {
      if (path === '/metrics/series') return { json: series() };
      if (path === '/models') return { json: { models: [INSTALLED_Q2], disk: DISK } };
      return undefined;
    },
  });
  await page.goto('/admin/status');
  await expect(page.locator('.chart .uplot canvas').first()).toBeVisible();
}

test.describe('Status charts on a Retina screen', () => {
  test.use({ deviceScaleFactor: 2, viewport: { width: 1280, height: 900 } });

  test('row 8: canvases are drawn at CSS size, not device-pixel size', async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'desktop viewport');
    await readyStatus(page);
    const sizes = await page.locator('.chart .uplot').evaluateAll((plots) =>
      plots.map((p) => ({ plot: p.getBoundingClientRect().width, canvas: p.querySelector('canvas')!.getBoundingClientRect().width, backing: p.querySelector('canvas')!.width })),
    );
    expect(sizes.length).toBeGreaterThan(0);
    for (const s of sizes) {
      expect(s.backing).toBeGreaterThan(s.plot * 1.5);
      expect(Math.abs(s.canvas - s.plot)).toBeLessThanOrEqual(1);
    }
  });

  test('row 8b: the memory chart’s y axis is wide enough for "55.9 GB"', async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'desktop viewport');
    await readyStatus(page);
    const chart = page.locator('.chart', { has: page.locator('.chart-title', { hasText: 'Memory' }) }).first();
    // Wait for the backfill to be drawn (the band redraws once a second).
    await expect(chart.locator('.chart-title')).toContainText('GB');
    const left = await chart.locator('.u-over').evaluate((el) => parseFloat(getComputedStyle(el).left));
    // uPlot's default y axis is 50 px; "55.9 GB" at 12 px Archivo needs more.
    expect(left).toBeGreaterThan(50);
  });
});

test('row 9: header separators never start a line', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'desktop width');
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockManager(page, { engine: READY });
  await page.goto('/admin/status');
  // The meta's own separators (the status chip's dot is aria-hidden too, so scope to the item groups).
  const dots = page.locator('.navband-meta .navband-item > [aria-hidden="true"]');
  await expect(dots.first()).toBeAttached();
  const orphans = await dots.evaluateAll((els) =>
    els.filter((dot) => {
      const prev = dot.previousElementSibling;
      if (!prev) return true;
      return Math.abs(prev.getBoundingClientRect().top - dot.getBoundingClientRect().top) > 8;
    }).length,
  );
  expect(orphans).toBe(0);
});

// ---------- Models (rows 11, 15, 23) ----------

test('row 11: the Disk band shows sizes, each legend item apart', async ({ page }) => {
  await mockManager(page, {
    engine: READY,
    extra: (_m, path) => {
      if (path === '/models') return { json: { models: [INSTALLED_Q2], disk: DISK } };
      if (path === '/storage') return { json: { ...DISK, tmp_dir: '/t', splash_data_dir: '/s', splash_data_bytes: 1024 ** 2, models_shared_with_hf_cache: false } };
      if (path === '/system') return { json: SYSTEM };
      if (path === '/downloads') return { json: { items: [] } };
      return undefined;
    },
  });
  await page.goto('/admin/models');
  const line = page.getByTestId('disk-band').locator('.disk-line');
  await expect(line).toContainText('Models 12.4 GB');
  await expect(line).toContainText('Cache 2 GB');
  await expect(line).toContainText('Free 279 GB of 931 GB');
  const gap = await line.evaluate((el) => getComputedStyle(el).columnGap);
  expect(parseFloat(gap)).toBeGreaterThan(0);
});

const GGUF_ENTRY = {
  id: GGUF,
  repo_id: GGUF,
  family: 'Qwen3.6-35B-A3B',
  format: 'gguf',
  size_bytes: 21.7 * GB,
  download_bytes: 22.6 * GB,
  fit: 'fits',
  installed: true,
  recommended: false,
  recommended_variant: 'UD-Q4_K_XL',
  variants: [
    { name: 'imatrix_unsloth', size_bytes: 0.9 * GB, loadable: false, reason: 'Not a model file' },
    { name: 'UD-Q2_K_XL', size_bytes: 13.3 * GB, download_bytes: 14.2 * GB, loadable: null },
    { name: 'UD-Q4_K_XL', size_bytes: 21.7 * GB, download_bytes: 22.6 * GB, loadable: null },
  ],
};

async function downloader(page: Page, posts: unknown[] = []) {
  await mockManager(page, {
    engine: READY,
    extra: (method, path, _url, body) => {
      if (path === '/catalog') return { json: { memory_bytes: SYSTEM.memory_bytes, families: [{ family: 'Qwen3.6-35B-A3B', label: 'Qwen3.6-35B-A3B', groups: [{ format: 'gguf', entries: [GGUF_ENTRY] }] }] } };
      if (path === '/models') return { json: { models: [INSTALLED_Q2], disk: DISK } };
      if (path === '/downloads' && method === 'POST') return posts.push(body), { json: { id: 'd1', model: 'x', state: 'queued', created_at: '2026-10-04T09:00:00Z', bytes_done: 0, language_only: false, verify: false } };
      if (path === '/downloads') return { json: { items: [] } };
      return undefined;
    },
  });
  await page.goto('/admin/models/downloader?tab=supported');
}

test('row 15: choosing a variant selects it; only the button downloads', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'desktop layout');
  const posts: unknown[] = [];
  await downloader(page, posts);
  const row = page.locator('.dlr-entry').first();
  await expect(row.getByRole('button', { name: 'Download UD-Q4_K_XL · 22.6 GB' })).toBeVisible();
  await row.getByRole('button', { name: 'Variant' }).click();
  await expect(page.getByRole('menuitemradio', { name: /imatrix_unsloth/ })).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByRole('menuitemradio', { name: /imatrix_unsloth/ })).toContainText('Not a model file');
  // Enter on the focused (checked) item must not start a download.
  await page.keyboard.press('Enter');
  await row.getByRole('button', { name: 'Variant' }).click();
  await page.getByRole('menuitemradio', { name: /UD-Q2_K_XL/ }).click();
  expect(posts).toEqual([]);
  // UD-Q2_K_XL is installed, so its Download is off; pick Q4 again and download it.
  await row.getByRole('button', { name: 'Variant' }).click();
  await page.getByRole('menuitemradio', { name: /UD-Q4_K_XL/ }).click();
  expect(posts).toEqual([]);
  await row.getByRole('button', { name: 'Download UD-Q4_K_XL · 22.6 GB' }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toMatchObject({ id: `${GGUF}:UD-Q4_K_XL` });
});

test('row 23: an installed GGUF repo says which variant is installed and still offers the recommended one', async ({ page }) => {
  await downloader(page);
  const row = page.locator('.dlr-entry').first();
  await expect(row.getByText('UD-Q2_K_XL installed')).toBeVisible();
  await expect(row.getByRole('button', { name: 'Load UD-Q2_K_XL' })).toBeVisible();
  await expect(row.getByRole('button', { name: 'Download UD-Q4_K_XL · 22.6 GB' })).toBeEnabled();
});

// ---------- Chat (rows 17, 18) ----------

let slow: Server;
let slowPort = 0;
test.beforeAll(async () => {
  // Streams one chunk, then holds the connection open until the client goes away: what a Stop interrupts.
  slow = createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'text/event-stream', 'access-control-allow-origin': '*' });
    res.write(`data: ${JSON.stringify({ choices: [{ delta: { role: 'assistant', content: 'Partial answer' }, index: 0 }] })}\n\n`);
    req.on('close', () => res.end());
  });
  await new Promise<void>((ok) => slow.listen(0, '127.0.0.1', ok));
  slowPort = (slow.address() as AddressInfo).port;
});
test.afterAll(() => new Promise<void>((ok) => slow.close(() => ok())));

function longChat(n: number) {
  const messages = Array.from({ length: n }, (_, i) => ({
    id: `m${i}`,
    parent: i ? `m${i - 1}` : null,
    role: i % 2 ? 'assistant' : 'user',
    content: `Message ${i}. `.repeat(20),
    created_at: '2026-10-04T09:00:00+00:00',
    ...(i % 2 ? { meta: { finish_reason: 'stop', model: CHAT.model } } : {}),
  }));
  return { ...CHAT, messages, active_leaf: `m${n - 1}` };
}

async function chatPage(page: Page, doc: unknown) {
  await mockManager(page, {
    engine: { ...READY, model: CHAT.model },
    extra: (method, path) => {
      if (path === '/chats' && method === 'GET') return { json: { chats: [] } };
      if (path === '/chats/c1') return { json: doc };
      if (path.endsWith('/profiles')) return { json: { model: CHAT.model, profiles: [], sampling_defaults: {} } };
      return undefined;
    },
  });
  await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
}

test('row 18: Stop ends the reply as stopped, with no "connection dropped" banner', async ({ page }) => {
  await chatPage(page, CHAT);
  await page.route('**/v1/chat/completions', (r) => r.continue({ url: `http://127.0.0.1:${slowPort}/v1/chat/completions` }));
  await page.goto('/admin/chat/c1');
  await page.getByRole('textbox', { name: 'Message' }).fill('go');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText('Partial answer')).toBeVisible();
  await page.getByRole('button', { name: 'Stop' }).click();
  await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
  await expect(page.getByText(/connection dropped/i)).toHaveCount(0);
  await expect(page.getByText('Partial answer')).toBeVisible();
});

test('row 17: on desktop the thread is the scrolling region and follows a new reply', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'desktop layout');
  await page.setViewportSize({ width: 1280, height: 860 });
  await chatPage(page, longChat(40));
  await page.route('**/v1/chat/completions', (r) =>
    r.fulfill({ headers: { 'content-type': 'text/event-stream' }, body: `data: ${JSON.stringify({ choices: [{ delta: { content: 'The newest reply.' }, index: 0, finish_reason: 'stop' }] })}\n\ndata: [DONE]\n\n` }),
  );
  await page.goto('/admin/chat/c1');
  const thread = page.getByTestId('chat-messages');
  await expect(thread).toContainText('Message 39.');
  const m = await thread.evaluate((el) => ({ overflow: getComputedStyle(el).overflowY, scrolls: el.scrollHeight > el.clientHeight, page: document.documentElement.scrollHeight - innerHeight }));
  expect(m.overflow).toBe('auto');
  expect(m.scrolls).toBe(true);
  expect(m.page).toBeLessThanOrEqual(1);
  // The composer stays on screen.
  await expect(page.getByRole('textbox', { name: 'Message' })).toBeInViewport();
  await page.getByRole('textbox', { name: 'Message' }).fill('next');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText('The newest reply.')).toBeInViewport();
});

// ---------- Tools, Integrations, Diagnostics (rows 19, 21, 22) ----------

test('row 19: judgment facts leave no dangling separator', async ({ page }) => {
  await mockManager(page, { engine: READY });
  await page.route('**/v1/systemone', (r) => r.fulfill({ json: { answers: { intent: { type: 'choice', choice: 'billing', probabilities: { billing: 0.8, other: 0.2 }, confidence: 0.28 } }, usage: { input_tokens: 120 } } }));
  await page.goto('/admin/tools/judgments');
  await page.getByRole('button', { name: 'Run', exact: true }).click();
  const facts = page.getByTestId('jd-answer-facts').first();
  await expect(facts).toContainText('Answer billing');
  expect((await facts.innerText()).trim()).not.toMatch(/·\s*$/);
  expect(await facts.innerText()).not.toContain('·');
});

test('row 21: URLs, paths and fix commands are mono and keep their case', async ({ page }) => {
  await mockManager(page, {
    engine: READY,
    extra: (_m, path) => {
      if (path === '/doctor') return { json: { ok: true, checks: [{ id: 'permissions', label: 'Private data directory', status: 'warn', message: '/Users/arnav/.splash', fix: 'chmod 700 /Users/arnav/.splash' }] } };
      if (path === '/storage') return { json: { ...DISK, tmp_dir: '/t', splash_data_dir: '/Users/arnav/Library/Application Support/Splash', splash_data_bytes: 1, models_shared_with_hf_cache: false } };
      if (path === '/settings') return { json: { ...SETTINGS, resolved: { base: '/Users/arnav/.splash', models_dir: '', cache_dir: '', tmp_dir: '', splash_data_dir: '', crash_trace_dir: '' } } };
      return undefined;
    },
  });
  await page.goto('/admin/logs/diagnostics');
  await page.getByRole('button', { name: 'Run doctor' }).click();
  const fix = page.locator('code.mono', { hasText: 'chmod 700 /Users/arnav/.splash' }).first();
  await expect(fix).toBeVisible();
  expect(await fix.evaluate((el) => getComputedStyle(el).textTransform)).toBe('none');
  await expect(page.getByText('Splash engine data')).toBeVisible();
  await expect(page.getByText('/Users/arnav/.splash/logs')).toBeVisible();
  await expect(page.getByText('~/.splash/logs')).toHaveCount(0);

  await page.goto('/admin/integrations');
  const url = page.locator('.integrations-meta .mono').first();
  await expect(url).toHaveText(/^http:\/\/127\.0\.0\.1:\d+$/);
  expect(await url.evaluate((el) => getComputedStyle(el).textTransform)).toBe('none');
});

test.describe('360 × 780 with long IDs and paths', () => {
  test.use({ viewport: { width: 360, height: 780 } });

  test('row 22: no horizontal scroll on any page with populated data', async ({ page }) => {
    await mockManager(page, {
      engine: READY,
      extra: (_m, path) => {
        if (path === '/system') return { json: SYSTEM };
        if (path === '/versions') return { json: { gui: '0.1.0', manager: '0.1.0', python: '3.12', engine: { ...DISCOVERY, cli: '/opt/homebrew/opt/splash/libexec/python/bin/splash-cli-entry-point-with-a-long-name' }, engine_update: { available: false } } };
        if (path === '/doctor') return { json: { ok: true, checks: [{ id: 'path', label: 'CLI PATH', status: 'warn', message: 'A splash function or alias hides the shim: /Users/someone-with-a-long-name/.zshrc', fix: null }] } };
        if (path === '/models') return { json: { models: [INSTALLED_Q2], disk: DISK } };
        if (path === '/downloads') return { json: { items: [] } };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: { object: 'list', data: [{ id: Q2, loaded: true, max_model_len: 262144 }] } }));
    for (const path of ['/admin/status', '/admin/models', '/admin/tools/playground', '/admin/tools/tokenizer', '/admin/tools/judgments', '/admin/integrations', '/admin/logs/diagnostics', '/admin/welcome?step=1', '/admin/chat']) {
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      const overflow = await page.evaluate(() => {
        const style = document.createElement('style');
        style.textContent = 'html,body{overflow-x:visible !important}';
        document.head.append(style);
        return document.body.scrollWidth - document.documentElement.clientWidth;
      });
      expect(overflow, `on ${path}`).toBeLessThanOrEqual(0);
    }
  });
});
