import { expect, test, type Page } from '@playwright/test';

import { CHAT, INTEGRATIONS, USAGE, V1_MODELS, mockManager } from './fixtures';

const READY = {
  state: 'ready',
  phase: null,
  model: CHAT.model,
  since: '2026-10-04T09:00:00+00:00',
  restart: { auto_restart: true },
  engine: { found: true, version: '1.2.0', support: 'supported' },
  maximum_context_tokens: 262144,
  requests_in_flight: 0,
};

async function noOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const style = document.createElement('style');
    style.textContent = 'html,body{overflow-x:visible !important}';
    document.head.append(style);
    return document.body.scrollWidth - document.documentElement.clientWidth;
  });
}

test.describe('usage history', () => {
  test('filters, export link, totals, pagination with a row range and injected fields', async ({ page }) => {
    const calls = await mockManager(page, { usage: USAGE });
    const urls: string[] = [];
    page.on('request', (r) => urls.push(r.url()));
    await page.goto('/admin/status/history?status=5xx&endpoint=/v1/messages');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(/Usage history\./i);
    // totals band
    await expect(page.getByRole('region', { name: 'Totals' })).toContainText('1,284');
    // export carries the active filters under the manager's names
    const href = await page.getByRole('link', { name: 'Export CSV' }).getAttribute('href');
    const exp = new URL(href!, 'http://x');
    expect(exp.pathname).toBe('/api/admin/usage/export.csv');
    expect(exp.searchParams.get('status')).toBe('5xx');
    expect(exp.searchParams.get('endpoint')).toBe('/v1/messages');
    expect(exp.searchParams.get('start')).toBeTruthy();
    expect(exp.searchParams.get('end')).toBeTruthy();
    // 50 rows, a row range, then the next page
    await expect(page.locator('.history-log tbody tr')).toHaveCount(50);
    await expect(page.getByText('Rows 1–50 of 1,284')).toBeVisible();
    await page.getByRole('button', { name: /Older/ }).click();
    await expect(page.getByText('Rows 51–100 of 1,284')).toBeVisible();
    await expect.poll(() => urls.some((u) => u.includes('/usage/requests') && u.includes('page=2'))).toBe(true);
    // injected fields: Tag "n fields" opens the JSON
    const tag = page.getByRole('button', { name: /Show the fields injected into request/ }).first();
    await expect(tag).toContainText('3 fields');
    await tag.click();
    await expect(page.locator('.history-injected pre').first()).toContainText('"temperature": 0.7');
    // the heatmap uses ink opacity 0.08–1.0
    const heat = await page.locator('.heat-cell').evaluateAll((els) => els.map((e) => Number((e as HTMLElement).style.getPropertyValue('--heat') || 0)));
    expect(Math.max(...heat)).toBe(1);
    expect(Math.min(...heat.filter((h) => h > 0))).toBeGreaterThanOrEqual(0.08);
    // top clients table with tokens and last seen
    await expect(page.getByRole('table', { name: 'Top clients' })).toContainText('claude-code/1.2.3');
    expect(calls).toContain('GET /usage/facets');
  });

  test('shows the empty state when nothing was recorded', async ({ page }) => {
    const empty = {
      ...USAGE,
      summary: { ...USAGE.summary, requests: 0, top_clients: [] },
      rows: { ...USAGE.rows, rows: [], total: 0, next_cursor: null },
    };
    await mockManager(page, { usage: empty as unknown as typeof USAGE });
    await page.goto('/admin/status/history');
    await expect(page.getByText('No requests recorded.')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open Chat' })).toBeVisible();
  });

  test('a failing band shows its own error and the others still render', async ({ page }) => {
    await mockManager(page, {
      usage: USAGE,
      extra: (_m, path) => (path === '/usage/timeseries' ? { status: 500, json: { error: { message: 'boom', type: 'server_error', code: 'x' } } } : undefined),
    });
    await page.goto('/admin/status/history');
    await expect(page.getByText('Couldn’t load usage over time.').first()).toBeVisible();
    await expect(page.locator('.history-log tbody tr')).toHaveCount(50);
  });
});

test.describe('integrations', () => {
  test('CLI rows, install link, remove entry, SDK tabs and the Tauri origin', async ({ page }, info) => {
    // Desktop layout (tabs, not the phone select); the phone project covers 360px below.
    test.skip(info.project.name === 'phone', 'desktop layout');
    const puts: unknown[] = [];
    const calls = await mockManager(page, {
      engine: READY,
      extra: (method, path, _url, body) => {
        if (path === '/integrations') return { json: INTEGRATIONS };
        if (path === '/integrations/hermes/entries' && method === 'DELETE') return { json: { removed: ['splash'], backups: ['/b'] } };
        if (path === '/settings' && method === 'PUT') {
          puts.push(body);
          return { json: { restart_required: false, applied: {} } };
        }
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.goto('/admin/integrations');
    await expect(page.getByRole('heading', { name: /Claude Code/ })).toBeVisible();
    await expect(page.locator('.integration-row')).toHaveCount(7);
    // a missing client links to Splash's INSTALL_URLS entry
    const opencode = page.locator('.integration-row', { has: page.getByRole('heading', { name: /OpenCode/ }) });
    await expect(opencode.getByRole('link', { name: /Install/ })).toHaveAttribute('href', 'https://opencode.ai/docs/');
    // D47/D60: how each client gets a profile's reasoning effort
    await expect(page.getByTestId('profiles-claude')).toContainText(':no-think as MAX_THINKING_TOKENS=0');
    await expect(page.getByTestId('profiles-codex')).toContainText('-c model_reasoning_effort');
    await expect(opencode.getByTestId('profiles-opencode')).toContainText('Honoured');
    await expect(page.getByTestId('profiles-hermes')).toContainText('passed as --reasoning');
    await expect(page.getByTestId('profiles-pi')).toContainText(':no-think as off');
    // profile IDs are case-sensitive: the mono spans must not inherit the .meta uppercase
    await expect(page.getByTestId('profiles-claude').locator('.mono', { hasText: ':no-think' })).toHaveCSS('text-transform', 'none');
    // picking a profile adds --model to every command
    await page.getByRole('combobox', { name: 'Model for every command and snippet on this page' }).selectOption(`${CHAT.model}:no-think`);
    await expect(page.getByText(`$ splash launch claude --model ${CHAT.model}:no-think`)).toBeVisible();
    // Hermes remove → confirmation → DELETE
    const hermes = page.locator('.integration-row', { has: page.getByRole('heading', { name: /Hermes/ }) });
    await hermes.getByRole('button', { name: 'Remove' }).click();
    await expect(page.getByRole('alertdialog')).toContainText('~/.hermes/profiles/splash/config.yaml');
    await page.getByRole('alertdialog').getByRole('button', { name: 'Remove' }).click();
    await expect.poll(() => calls).toContain('DELETE /integrations/hermes/entries');
    // untested Codex version tag and the default-model toggle
    await expect(page.getByText('Untested version')).toBeVisible();
    await expect(page.getByRole('switch', { name: 'Make Splash the default model in Codex app' })).toBeVisible();
    // seven SDK tabs; Jan / Tauri appends exactly one origin
    await expect(page.getByRole('tab')).toHaveCount(7);
    await page.getByRole('tab', { name: 'Jan / Tauri' }).click();
    await page.getByRole('button', { name: 'Allow origin tauri://localhost' }).click();
    await expect.poll(() => puts.length).toBeGreaterThan(0);
    const doc = puts[0] as { global: { server: { allowed_origins: string[] } } };
    expect(doc.global.server.allowed_origins).toEqual(['tauri://localhost']);
  });

  test('a closed tooltip near the right edge does not widen the desktop page', async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'desktop layout');
    await page.setViewportSize({ width: 1280, height: 900 });
    await mockManager(page, { engine: READY, extra: (_m, path) => (path === '/integrations' ? { json: INTEGRATIONS } : undefined) });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.goto('/admin/integrations');
    // Codex app is an untested version: its Tag carries a Tooltip at the right edge of the row.
    await expect(page.getByRole('tooltip', { includeHidden: true }).first()).toBeAttached();
    expect(await noOverflow(page)).toBeLessThanOrEqual(0);
  });
});

test.describe('integrations deep link', () => {
  test('?connect=claude-desktop opens the connect sheet with the restart warning', async ({ page }) => {
    const calls = await mockManager(page, {
      engine: READY,
      extra: (method, path) => {
        if (path === '/integrations') return { json: INTEGRATIONS };
        if (path === '/integrations/claude-desktop/connect' && method === 'POST')
          return { json: { ...INTEGRATIONS.desktop[0], state: 'connected', connected_at: '2026-10-04T09:02:00' } };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.goto('/admin/integrations?connect=claude-desktop');
    const sheet = page.getByRole('dialog', { name: 'Connect Claude Desktop.' });
    await expect(sheet).toContainText('Claude Desktop will restart. Your previous configuration is backed up and restored when you disconnect or quit Splashboard.');
    await expect(sheet).toContainText('Unsaved work in Claude Desktop will be lost.');
    await sheet.getByRole('button', { name: /Quit, connect and reopen/ }).click();
    await expect.poll(() => calls).toContain('POST /integrations/claude-desktop/connect');
    await expect(page.getByText('Connected since 09:02')).toBeVisible();
  });
});

test.describe('settings', () => {
  const SCHEMA = {
    sections: [],
    fields: [
      { key: 'server.port', label: 'Port', help: 'Where the API listens.', section: 'server_network', control: 'number', applies: 'restart', scope: 'G', flag: '--port', advanced: false, storage: 'settings', default: 8000, disabled_for_legacy: false },
    ],
    profile_fields: [],
    engine_options: { version: '1.2.0', options: [], unknown: [] },
  };
  const extra = (method: string, path: string) => {
    if (path === '/settings/schema') return { json: SCHEMA };
    if (path === '/settings/effective') return { json: { model: null, values: {} } };
    if (path === '/data/sizes')
      return {
        json: {
          targets: [
            { target: 'chats', label: 'Chat history', bytes: 18_400_000, items: 42, note: '~/.splash/chats' },
            { target: 'kv_cache', label: 'Persistent KV cache', bytes: 9_600_000_000, items: null, note: 'stops the engine first' },
          ],
        },
      };
    if (path === '/versions')
      return { json: { gui: '0.1.0', manager: '0.1.0', python: '3.12.6', engine: { found: true, version: '1.2.0', support: 'supported', supported_range: '>=1.2', source_checkout: false, cli: '/opt/homebrew/bin/splash' }, status_schema_version: 6, engine_update: { available: false, checked_at: '2026-10-04T08:00:00+00:00' } } };
    if (path === '/system')
      return { json: { chip: 'Apple M5 Pro', cpu_cores: 14, gpu_cores: 20, memory_bytes: 68719476736, macos_version: '27.0', arch: 'arm64', hostname: 'mac', supported: true, disk: {}, power: { source: 'ac' } } };
    if (method === 'GET' && path === '/models') return { json: { models: [] } };
    return undefined;
  };

  test('redirects to the first section and lists all 17 in order', async ({ page }) => {
    await mockManager(page, { extra });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/settings');
    await expect(page).toHaveURL(/\/admin\/settings\/server$/);
    const nav = page.getByRole('navigation', { name: 'Settings sections' });
    await expect(nav.getByRole('link')).toHaveCount(17);
    await expect(nav.getByRole('link', { name: /Server & network/ })).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('[data-key="server.port"]')).toBeVisible();
  });

  test('at phone width a section select replaces the list', async ({ page }) => {
    await mockManager(page, { extra });
    await page.setViewportSize({ width: 360, height: 780 });
    await page.goto('/admin/settings/cache');
    await expect(page.getByRole('navigation', { name: 'Settings sections' }).getByRole('link').first()).toBeHidden();
    const select = page.getByRole('combobox', { name: 'Section' });
    await expect(select).toBeVisible();
    await select.selectOption('about');
    await expect(page).toHaveURL(/\/admin\/settings\/about$/);
  });

  test('Data & privacy confirmations state the size; About links the wizard', async ({ page }) => {
    await mockManager(page, { extra });
    await page.goto('/admin/settings/data');
    await page.getByRole('button', { name: 'Clear… Persistent KV cache' }).click();
    const sheet = page.getByRole('alertdialog');
    await expect(sheet).toContainText('8.9 GB will be deleted.');
    await expect(sheet.getByRole('button', { name: /Clear · 8\.9 GB/ })).toBeDisabled();
    await sheet.getByRole('textbox').fill('CLEAR');
    await expect(sheet.getByRole('button', { name: /Clear · 8\.9 GB/ })).toBeEnabled();
    await page.keyboard.press('Escape');
    await page.goto('/admin/settings/about');
    await expect(page.getByText('0.1.0 · manager 0.1.0 · Python 3.12.6')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Re-run welcome wizard' })).toHaveAttribute('href', '/admin/welcome?step=1');
    await expect(page.getByText(/Up to date · checked/)).toBeVisible();
  });
});

test.describe('chat', () => {
  test('/admin/chat/:cid opens the conversation and a send streams a reply', async ({ page }) => {
    let sent: Record<string, unknown> | null = null;
    await mockManager(page, {
      engine: READY,
      extra: (method, path) => {
        if (path === '/chats' && method === 'GET') return { json: { chats: [{ id: 'c1', title: CHAT.title, created_at: CHAT.created_at, updated_at: CHAT.updated_at, model: CHAT.model, profile: 'default', message_count: 2, snippet: null }] } };
        if (path === '/chats/c1' && method === 'GET') return { json: CHAT };
        if (path === '/chats/c1' && method === 'PUT') return { json: CHAT };
        if (path.endsWith('/profiles')) return { json: { model: CHAT.model, profiles: [{ name: 'default', id: CHAT.model, builtin: true, modified: false, overlay: {} }], sampling_defaults: {} } };
        if (path === '/usage/requests') return { json: { rows: [{ injected: { temperature: 0.7, top_p: 0.8 } }], next_cursor: null, total: 1, limit: 1 } };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.route('**/v1/chat/completions', async (r) => {
      sent = r.request().postDataJSON() as Record<string, unknown>;
      const chunks = [
        { choices: [{ delta: {}, index: 0 }], prompt_progress: { total: 32000, cache: 8192, processed: 12288, time_ms: 10 } },
        { choices: [{ delta: { role: 'assistant', content: 'Hi ' }, index: 0 }] },
        { choices: [{ delta: { content: 'there.' }, index: 0, finish_reason: 'stop' }], timings: { predicted_per_second: 70.5 } },
        { choices: [], usage: { prompt_tokens: 20, completion_tokens: 3 } },
      ];
      await r.fulfill({ headers: { 'content-type': 'text/event-stream', 'x-splash-request-id': 'r1' }, body: chunks.map((c) => `data: ${JSON.stringify(c)}\n\n`).join('') + 'data: [DONE]\n\n' });
    });
    await page.goto('/admin/chat/c1');
    await expect(page.getByText('Here are the action items.')).toBeVisible();
    await expect(page.getByText('In 12,288 (8,192 cached)')).toBeVisible();
    await page.getByRole('textbox', { name: 'Message' }).fill('hi');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect(page.getByText('Hi there.')).toBeVisible();
    // the proxy's injected defaults come from the usage row of x-splash-request-id (G4)
    await expect(page.getByText('+2 defaults')).toBeVisible();
    expect(sent).not.toBeNull();
    const body = sent as unknown as Record<string, unknown>;
    expect(body.stream).toBe(true);
    expect(body.return_progress).toBe(true);
    expect(body.stream_options).toEqual({ include_usage: true });
    expect(body.temperature).toBeUndefined();
    expect(body.model).toBe(CHAT.model);
  });

  test('a busy engine keeps the message, counts down and can wait for idle', async ({ page }) => {
    const headers: Array<Record<string, string>> = [];
    await mockManager(page, {
      engine: READY,
      extra: (method, path) => {
        if (path === '/chats' && method === 'GET') return { json: { chats: [] } };
        if (path === '/chats' && method === 'POST') return { status: 201, json: { ...CHAT, id: 'c9', messages: [], active_leaf: null } };
        if (path.startsWith('/chats/c9')) return { json: CHAT };
        if (path.endsWith('/profiles')) return { json: { model: CHAT.model, profiles: [], sampling_defaults: {} } };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.route('**/v1/chat/completions', async (r) => {
      headers.push(r.request().headers());
      if (headers.length === 1)
        return r.fulfill({
          status: 503,
          headers: { 'Retry-After': '10' },
          json: { error: { message: 'Splashboard is serving x; switching models while requests are in flight is disabled', type: 'overloaded_error', code: 'model_switch_busy' } },
        });
      return r.fulfill({ headers: { 'content-type': 'text/event-stream' }, body: `data: ${JSON.stringify({ choices: [{ delta: { content: 'ok' }, index: 0, finish_reason: 'stop' }] })}\n\ndata: [DONE]\n\n` });
    });
    await page.goto('/admin/chat');
    await page.getByRole('textbox', { name: 'Message' }).fill('hello');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect(page.getByText(/Splash is busy serving/)).toBeVisible();
    await expect(page.getByRole('button', { name: /Retry in \d+ s/ })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('hello');
    await page.getByRole('button', { name: 'Switch when idle' }).click();
    await expect(page.getByText('ok', { exact: true })).toBeVisible();
    expect(headers[1]?.['x-splash-switch']).toBe('wait');
  });

  test('a bad sampling value blocks send with the spec copy', async ({ page }) => {
    await mockManager(page, {
      engine: READY,
      extra: (method, path) => {
        if (path === '/chats' && method === 'GET') return { json: { chats: [] } };
        if (path.endsWith('/profiles')) return { json: { model: CHAT.model, profiles: [], sampling_defaults: {} } };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto('/admin/chat');
    await page.getByLabel('Temperature').fill('2.5');
    await expect(page.getByText('Temperature must be between 0 and 2.')).toBeVisible();
    await page.getByRole('textbox', { name: 'Message' }).fill('hi');
    await expect(page.getByRole('button', { name: 'Send' })).toBeDisabled();
  });
});

test.describe('not found', () => {
  test('an unknown admin path shows the 404 page with the path', async ({ page }) => {
    await mockManager(page);
    await page.goto('/admin/does-not-exist');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.locator('code', { hasText: '/admin/does-not-exist' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Status' }).last()).toBeVisible();
  });
});

test.describe('phone width, populated', () => {
  test('no horizontal scroll at 360px on chat with a long GGUF model ID (acceptance 2026-10-04)', async ({ page }) => {
    const LONG = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL';
    await mockManager(page, {
      engine: { ...READY, model: LONG },
      extra: (method, p) => {
        if (p === '/chats' && method === 'GET') return { json: { chats: [] } };
        if (p === '/chats/c1') return { json: { ...CHAT, model: LONG } };
        if (p.endsWith('/profiles')) return { json: { model: LONG, profiles: [], sampling_defaults: {} } };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) =>
      r.fulfill({ json: { object: 'list', data: [{ id: LONG, loaded: true, max_model_len: 262144, vision: true }, { id: `${LONG}:no-think`, root: LONG, profile: 'no-think', loaded: true }] } }),
    );
    await page.setViewportSize({ width: 360, height: 780 });
    await page.goto('/admin/chat/c1');
    await page.waitForLoadState('networkidle');
    await expect(page.getByTestId('chat-model')).toContainText('UD-Q2_K_XL');
    expect(await noOverflow(page)).toBeLessThanOrEqual(0);
    const box = await page.getByTestId('chat-model').boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(360);
  });

  for (const path of ['/admin/settings/about', '/admin/settings/data', '/admin/chat/c1', '/admin/does-not-exist', '/admin/integrations']) {
    test(`no horizontal scroll at 360px on ${path}`, async ({ page }) => {
      await mockManager(page, {
        engine: READY,
        extra: (method, p) => {
          if (p === '/integrations') return { json: INTEGRATIONS };
          if (p === '/chats' && method === 'GET') return { json: { chats: [] } };
          if (p === '/chats/c1') return { json: CHAT };
          return undefined;
        },
      });
      await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
      await page.setViewportSize({ width: 360, height: 780 });
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      expect(await noOverflow(page), path).toBeLessThanOrEqual(0);
    });
  }
});
