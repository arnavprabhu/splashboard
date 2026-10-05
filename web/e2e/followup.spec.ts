/** The routes that landed in 5b1c9ad, wired into the pages (docs/progress/session3-frontend.md "Follow-up"). */
import { expect, test } from '@playwright/test';

import { CHAT, INTEGRATIONS, PRINT_CLAUDE, PRINT_HERMES, V1_MODELS, mockManager } from './fixtures';

const MODEL = CHAT.model;
const READY = { state: 'ready', model: MODEL, engine: { found: true, version: '1.2.0', support: 'supported' }, maximum_context_tokens: 262144, requests_in_flight: 0 };
const err = (status: number, code: string, message = code) => ({ status, json: { error: { message, type: 'x', code } } });

test.describe('integrations follow-up', () => {
  test.beforeEach(async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'desktop layout');
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
  });

  test('"What this changes" comes from /print for the picked model; static ones are labelled; View backup', async ({ page }) => {
    const urls: string[] = [];
    const calls = await mockManager(page, {
      engine: READY,
      extra: (_method, path, url) => {
        if (path === '/integrations') return { json: INTEGRATIONS };
        if (path === '/integrations/claude/print') return urls.push(url.search), { json: PRINT_CLAUDE };
        if (path === '/integrations/hermes/print') return { json: PRINT_HERMES };
        if (path === '/integrations/hermes/reveal-backup') return { json: { ok: true, path: '/b' } };
        if (path === '/integrations/pi/reveal-backup') return err(404, 'no_backup');
        return undefined;
      },
    });
    await page.goto('/admin/integrations');
    const claude = page.locator('.integration-row', { has: page.getByRole('heading', { name: /Claude Code/ }) });
    await claude.getByText('What this changes').click();
    await expect(claude.getByText('ANTHROPIC_API_KEY')).toBeVisible();
    await expect(claude.getByText('key hidden')).toBeVisible();
    await expect(claude.getByText(/--permission-mode default/)).toBeVisible();
    expect(new URLSearchParams(urls[0]).get('model')).toBe(MODEL);
    const hermes = page.locator('.integration-row', { has: page.getByRole('heading', { name: /Hermes/ }) });
    await hermes.getByText('What this changes').click();
    await expect(hermes.getByText('Static description')).toBeVisible();
    await expect(hermes.getByRole('table')).toContainText('~/.hermes/profiles/splash/config.yaml');
    await hermes.getByRole('button', { name: 'View backup' }).click();
    await expect.poll(() => calls).toContain('POST /integrations/hermes/reveal-backup');
    const pi = page.locator('.integration-row', { has: page.getByRole('heading', { name: /^.*Pi$/ }) });
    await pi.getByText('What this changes').click();
    await pi.getByRole('button', { name: 'View backup' }).click();
    await expect(page.getByText('No backup yet.')).toBeVisible();
  });

  test('connect shows the steps and a failed step offers Restore now; Open app reports errors', async ({ page }) => {
    let connects = 0;
    const calls = await mockManager(page, {
      engine: READY,
      extra: (method, path) => {
        if (path === '/integrations') return { json: INTEGRATIONS };
        if (path === '/integrations/codex-app/connect' && method === 'POST')
          return connects++, { json: { ...INTEGRATIONS.desktop[1], state: 'not_connected', step: 'failed', message: 'Quit Codex and try again' } };
        if (path === '/integrations/codex-app/open') return err(503, 'app_open_failed', 'open -a failed');
        if (path === '/integrations/claude-desktop/open') return { json: { ok: true, path: '/Applications/Claude.app' } };
        return undefined;
      },
    });
    await page.goto('/admin/integrations');
    const codex = page.locator('.integration-row', { has: page.getByRole('heading', { name: /Codex app/ }) });
    await codex.getByRole('button', { name: 'Connect' }).click();
    const sheet = page.getByRole('dialog', { name: 'Connect Codex app.' });
    await sheet.getByRole('button', { name: 'Connect' }).click();
    await expect(sheet.getByTestId('steps-connect')).toContainText('Quitting the app');
    await expect(sheet.getByTestId('steps-connect')).toContainText('Writing config');
    await expect(sheet.getByTestId('steps-connect')).not.toContainText('Starting the gateway');
    await expect(sheet).toContainText('Quit Codex and try again');
    await expect(sheet.getByRole('button', { name: 'Restore now' })).toBeVisible();
    expect(connects).toBe(1);
    await page.keyboard.press('Escape');
    await codex.getByRole('button', { name: 'Open app' }).click();
    await expect(page.getByText('Couldn’t open the app.')).toBeVisible();
    const claudeDesktop = page.locator('.integration-row', { has: page.getByRole('heading', { name: /Claude Desktop/ }) });
    await claudeDesktop.getByRole('button', { name: 'Open app' }).click();
    await expect.poll(() => calls).toContain('POST /integrations/claude-desktop/open');
  });

  test('Restart Codex re-applies the connection after the default-model toggle', async ({ page }) => {
    const connected = { ...INTEGRATIONS.desktop[1], state: 'connected', connected_at: '2026-10-04T09:02:00' };
    const calls = await mockManager(page, {
      engine: READY,
      extra: (method, path) => {
        if (path === '/integrations') return { json: { ...INTEGRATIONS, desktop: [INTEGRATIONS.desktop[0], connected] } };
        if (path === '/settings' && method === 'PUT') return { json: { restart_required: false, applied: {} } };
        if (path === '/integrations/codex-app/disconnect') return { json: { ...connected, state: 'not_connected', step: 'done' } };
        if (path === '/integrations/codex-app/connect') return { json: { ...connected, step: 'done' } };
        return undefined;
      },
    });
    await page.goto('/admin/integrations');
    await page.getByRole('switch', { name: 'Make Splash the default model in Codex app' }).click();
    await page.getByRole('button', { name: 'Restart Codex' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Restart Codex' }).click();
    await expect(page.getByText('Codex restarted with the new default.')).toBeVisible();
    const order = calls.filter((c) => c.includes('codex-app/'));
    expect(order).toEqual(['POST /integrations/codex-app/disconnect', 'POST /integrations/codex-app/connect']);
  });

  test('the slots editor previews the gateway /v1/models answer', async ({ page }) => {
    await mockManager(page, { engine: READY, extra: (_m, path) => (path === '/integrations' ? { json: INTEGRATIONS } : undefined) });
    await page.goto('/admin/integrations');
    await page.locator('summary', { hasText: /Model slots$/ }).click();
    await page.getByText('Show response').click();
    await expect(page.locator('pre', { hasText: '"anthropic_family_tier": "sonnet"' })).toContainText(`"display_name": "${MODEL}"`);
  });
});

test.describe('settings follow-up', () => {
  const SCHEMA = { sections: [], fields: [], profile_fields: [], engine_options: { available: true, version: '1.2.0', unknown: [] } };
  test('the API key shows the masked value from secret meta', async ({ page }) => {
    await mockManager(page, {
      extra: (_m, path, url) => {
        if (path === '/settings') return { json: { settings: { version: 1, global: { wizard: { completed: true } }, models: {} }, secrets: { api_key_set: true, hf_token_override_set: false, hf_login_token_present: false }, resolved: {}, read_only: false, load_warnings: [] } };
        if (path === '/settings/schema') return { json: SCHEMA };
        if (path === '/settings/secret/meta') return { json: { name: url.searchParams.get('name'), set: true, prefix: 'sk-splash-', last4: 'a91f', masked: 'sk-splash-••••a91f', updated_at: null } };
        return undefined;
      },
    });
    await page.goto('/admin/settings/security');
    await expect(page.getByTestId('api-key-masked')).toHaveText('sk-splash-••••a91f');
  });

  test('Reset all settings needs RESET, retries with force when busy and lists what was kept', async ({ page }) => {
    const bodies: unknown[] = [];
    await mockManager(page, {
      extra: (method, path, _url, body) => {
        if (path === '/settings/schema') return { json: SCHEMA };
        if (path === '/settings/reset' && method === 'POST') {
          bodies.push(body);
          if (bodies.length === 1) return err(409, 'model_switch_busy');
          return { json: { settings: { version: 1, global: {}, models: {} }, restart_required: true, engine_restarted: true, kept: ['global.storage', 'global.wizard', 'global.chat.mcp_servers'] } };
        }
        return undefined;
      },
    });
    await page.goto('/admin/settings/advanced');
    await page.getByRole('button', { name: 'Reset all settings' }).click();
    const sheet = page.getByRole('alertdialog');
    await sheet.getByRole('textbox').fill('RESET');
    await sheet.getByRole('button', { name: 'Reset all settings' }).click();
    await expect(sheet.getByText(/Requests are running/)).toBeVisible();
    await sheet.getByRole('button', { name: 'Reset & restart anyway' }).click();
    await expect(page.getByTestId('reset-kept')).toContainText('global.chat.mcp_servers');
    expect(bodies).toEqual([{ restart_engine: true, force: false }, { restart_engine: true, force: true }]);
  });
});

test.describe('models follow-up', () => {
  test('Download by ID shows the download plan and blocks when it does not fit', async ({ page }) => {
    const plan = {
      variant: null,
      language_only: false,
      files: [
        { name: 'model.safetensors', repo_id: 'mlx-community/Qwen3.8-27B-4bit', bytes: 15_900_000_000, present: false },
        { name: 'model.safetensors', repo_id: 'incoai/Qwen3.8-27B-DFlash2', bytes: 700_000_000, present: true },
      ],
      total_bytes: 16_600_000_000,
      remaining_bytes: 15_900_000_000,
      free_bytes: 10_000_000_000,
      margin_bytes: 2_147_483_648,
      fits_on_disk: false,
    };
    await mockManager(page, {
      extra: (_m, path) => {
        if (path === '/inspect')
          return { json: { id: 'mlx-community/Qwen3.8-27B-4bit', repo_id: 'mlx-community/Qwen3.8-27B-4bit', compatible: true, badge: 'compatible', family: 'Qwen3.8-27B', format: 'mlx', variants: [], vision: { available: true }, cached: false, checked_at: '', download_plan: plan, language_only_plan: { ...plan, language_only: true } } };
        return undefined;
      },
    });
    await page.goto('/admin/models/downloader?tab=id&id=mlx-community/Qwen3.8-27B-4bit');
    await expect(page.getByText('HF token: no token · gated repos unavailable')).toBeVisible();
    const box = page.getByTestId('download-plan');
    await expect(box).toContainText('already on disk');
    // Hub sizes are decimal everywhere a download is sized (wizard, catalog, plan, downloads panel).
    await expect(box).toContainText('15.9 GB to download of 16.6 GB');
    await expect(page.getByText(/Not enough space/)).toBeVisible();
    await expect(page.getByRole('button', { name: /Download · 15\.9 GB/ })).toBeDisabled();
  });

  test('per-model Info shows the fingerprints of the last load', async ({ page }) => {
    await mockManager(page, {
      extra: (_m, path) => {
        if (path.startsWith('/models/mlx-community/') && !path.endsWith('/profiles'))
          return { json: { id: MODEL, repo_id: MODEL, format: 'mlx', language_only: false, size_bytes: 1, unique_bytes: 1, pinned: false, legacy: false, status: 'ready', update_available: false, fingerprints: { build_id: 'b-42', loaded_model_layout_sha256: 'abc123', target_model_sha256: 'def456', kv_format: 'int8', kv_quantization: 'symmetric_int8', max_context: 131072, recorded_at: '2026-10-04T09:00:00Z' } } };
        return undefined;
      },
    });
    await page.goto(`/admin/models/${MODEL}/settings?section=info`);
    await expect(page.getByText('b-42')).toBeVisible();
    await expect(page.getByText('int8 / symmetric_int8')).toBeVisible();
    await expect(page.getByText('128K')).toBeVisible();
  });
});

test.describe('diagnostics follow-up', () => {
  test('replay while the engine runs asks first, Stop cancels, paths show permissions', async ({ page }) => {
    const calls = await mockManager(page, {
      engine: READY,
      extra: (_m, path) => {
        if (path === '/traces') return { json: { enabled: true, directory: '~/Library/Logs/Splash/crash', traces: [{ name: 'splash-crash-g1-1.json', path: '/t', size_bytes: 1000, modified_at: '2026-10-04T09:00:00Z' }] } };
        if (path === '/traces/splash-crash-g1-1.json/replay/cancel') return { json: { cancelled: true } };
        if (path === '/doctor') return { json: { ok: true, checks: [{ id: 'permissions', label: 'Permissions', status: 'ok', message: '~/.splash 0700, settings.json 0600', fix: null }] } };
        if (path === '/storage') return { json: { models_dir: '/m', cache_dir: '/c', tmp_dir: '/t', splash_data_dir: '/s' } };
        return undefined;
      },
    });
    let release: () => void = () => undefined;
    await page.route('**/api/admin/traces/*/replay', async (r) => {
      await new Promise<void>((res) => (release = res));
      await r.fulfill({ headers: { 'content-type': 'text/event-stream' }, body: 'event: line\ndata: {"text":"replaying"}\n\nevent: exit\ndata: {"code":-15}\n\n' });
    });
    await page.goto('/admin/logs/diagnostics');
    await expect(page.getByText('~/.splash 0700, settings.json 0600')).toBeVisible();
    await page.getByRole('button', { name: 'Replay' }).click();
    const sheet = page.getByRole('alertdialog', { name: 'Replay while the engine runs.' });
    await sheet.getByRole('button', { name: 'Replay anyway' }).click();
    await page.getByRole('button', { name: 'Stop' }).click();
    await expect.poll(() => calls).toContain('POST /traces/splash-crash-g1-1.json/replay/cancel');
    release();
    await expect(page.getByText('Exited with code -15.')).toBeVisible();
  });
});

test.describe('tokenizer follow-up', () => {
  test('pieces come from /tokenizer/pieces; 503 keeps IDs only', async ({ page }) => {
    let pieces = true;
    await mockManager(page, {
      engine: READY,
      extra: (_m, path) => {
        if (path === '/tokenizer/pieces') return pieces ? { json: { model: MODEL, pieces: [{ id: 785, piece: 'The', text: 'The' }, { id: 3974, piece: 'Ġquick', text: ' quick' }] } } : err(503, 'pieces_unavailable');
        return undefined;
      },
    });
    await page.route('**/tokenize', (r) => r.fulfill({ json: { tokens: [785, 3974] } }));
    await page.goto('/admin/tools/tokenizer');
    await page.getByRole('button', { name: 'Tokenize' }).last().click();
    await expect(page.locator('.token-piece').nth(1)).toContainText('␣quick');
    pieces = false;
    await page.getByRole('button', { name: 'Tokenize' }).last().click();
    await expect(page.getByText('Pieces are unavailable for this model; showing IDs only.')).toBeVisible();
    await expect(page.locator('.token-piece').first()).toHaveText('785');
  });
});

test.describe('wizard follow-up', () => {
  test('a gated download offers the token sheet with whoami status', async ({ page }) => {
    await page.addInitScript((m) => localStorage.setItem('splash-gui-wizard', JSON.stringify({ step: 4, reached: 4, pendingPort: null, preset: 'chat', model: m, downloadId: 'd1' })), MODEL);
    let who = { status: 'no_token', source: 'none', user: null, orgs: [], http_status: null, message: null } as Record<string, unknown>;
    await mockManager(page, {
      extra: (method, path) => {
        if (path === '/downloads') return { json: { items: [{ id: 'd1', model: MODEL, language_only: false, verify: false, state: 'failed', created_at: '2026-10-04T09:00:00Z', bytes_done: 0, error: { code: 'gated', message: '401', action: 'add_hf_token' } }] } };
        if (path === '/settings/presets') return { json: { memory_bytes: 68719476736, presets: [{ id: 'chat', label: 'Chat & general', description: 'd', settings: {}, recommendation: { primary: { model: MODEL, note: 'best' }, alternatives: [], reason: '' } }] } };
        if (path === '/hf/whoami') return { json: who };
        if (path === '/settings/secrets/hf-token' && method === 'PUT') {
          who = { status: 'ok', source: 'override', user: 'arnav', orgs: [], http_status: 200, message: null };
          return { json: { api_key_set: false, hf_token_override_set: true, hf_login_token_present: false } };
        }
        return undefined;
      },
    });
    await page.goto('/admin/welcome?step=4');
    await expect(page.getByText('Gated or private: add a Hugging Face token.').first()).toBeVisible();
    await page.getByRole('button', { name: 'Add token' }).click();
    const sheet = page.getByRole('dialog', { name: 'Hugging Face token.' });
    await expect(sheet.getByTestId('whoami')).toHaveText('No token yet.');
    await sheet.getByLabel('Token').fill('hf_abcdefgh');
    await sheet.getByRole('button', { name: 'Save token' }).click();
    await expect(sheet.getByTestId('whoami')).toContainText('Signed in as arnav ✓');
    await expect(sheet.getByRole('button', { name: 'Retry download' })).toBeEnabled();
  });
});
