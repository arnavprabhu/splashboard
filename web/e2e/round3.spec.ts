/** Owner decisions D40, D43 and D44 (docs/progress/session3-frontend.md "Round 3"). */
import { expect, test } from '@playwright/test';

import { CHAT, INTEGRATIONS, MCP_SERVERS, SETTINGS, V1_MODELS, mockManager } from './fixtures';

const READY = { state: 'ready', model: CHAT.model, engine: { found: true, version: '1.2.0', support: 'supported' }, maximum_context_tokens: 262144, requests_in_flight: 0 };
const field = (key: string, section: string, control: string, extra: Record<string, unknown> = {}) => ({
  key, label: key, help: '', section, control, applies: 'live', scope: 'G', flag: null, advanced: false, storage: 'settings', default: null, disabled_for_legacy: false, ...extra,
});
const SCHEMA = {
  sections: [],
  fields: [field('chat.mcp_servers', 'chat_mcp', 'custom'), field('chat.default_system', 'chat_mcp', 'text', { label: 'Default system prompt', default: '' })],
  profile_fields: [],
  engine_options: { version: '1.2.0', options: [], unknown: [] },
};

test.describe('D40 repository links', () => {
  test('About links the Splash GUI repository and issues', async ({ page }) => {
    await mockManager(page, { extra: (_m, path) => (path === '/settings/schema' ? { json: SCHEMA } : undefined) });
    await page.goto('/admin/settings/about');
    const links = page.getByTestId('about-gui-links');
    await expect(links.getByRole('link', { name: /^Repository/ })).toHaveAttribute('href', 'https://github.com/arnavprabhu/splash-gui');
    await expect(links.getByRole('link', { name: /^Issues/ })).toHaveAttribute('href', 'https://github.com/arnavprabhu/splash-gui/issues');
  });
});

test.describe('D43 MCP secrets', () => {
  test('masked values show, replacing is write-only and untouched values go back as the same object', async ({ page }) => {
    const puts: Array<{ servers: Record<string, Record<string, unknown>> }> = [];
    await mockManager(page, {
      extra: (method, path, _url, body) => {
        if (path === '/settings/schema') return { json: SCHEMA };
        if (path === '/mcp/servers' && method === 'PUT') {
          puts.push(body as (typeof puts)[number]);
          return { json: { servers: MCP_SERVERS } };
        }
        return undefined;
      },
    });
    await page.goto('/admin/settings/chat');
    const table = page.locator('[data-key="chat.mcp_servers"] table');
    await expect(table).toContainText('GITHUB_TOKEN ghp_…••••a1b2');
    await expect(table).toContainText('Authorization Bearer…••••9f3c');
    // The wider table scrolls inside its wrapper; the page itself never scrolls sideways.
    const width = page.viewportSize()!.width;
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);

    // A toggle sends every saved value back unchanged.
    await page.getByRole('switch', { name: 'Always allow · github' }).click();
    await expect.poll(() => puts.length).toBe(1);
    expect(puts[0]!.servers.github!.env).toEqual(MCP_SERVERS.github.env);
    expect(puts[0]!.servers.search!.headers).toEqual(MCP_SERVERS.search.headers);

    // Edit: the saved value is shown masked; pasting the masked text is refused.
    await page.getByRole('button', { name: 'Edit github' }).click();
    const sheet = page.getByRole('dialog', { name: 'Edit MCP server.' });
    const rows = sheet.getByTestId('mcp-secrets-env');
    await expect(rows).toContainText('ghp_…••••a1b2');
    await rows.getByRole('button', { name: 'Replace' }).click();
    const input = sheet.getByLabel('New value for GITHUB_TOKEN');
    await expect(input).toHaveAttribute('type', 'password');
    await expect(input).toHaveValue('');
    await input.fill('ghp_…••••a1b2');
    await expect(sheet.getByText('That is the masked value.', { exact: false })).toBeVisible();
    await expect(sheet.getByRole('button', { name: 'Save server' })).toBeDisabled();
    await input.fill('ghp_realtoken');
    await sheet.getByLabel('Add environment (KEY=value per line)').fill('LOG_LEVEL=debug');
    await sheet.getByRole('button', { name: 'Save server' }).click();
    await expect.poll(() => puts.length).toBe(2);
    expect(puts[1]!.servers.github!.env).toEqual({ GITHUB_TOKEN: 'ghp_realtoken', LOG_LEVEL: 'debug' });
    expect(puts[1]!.servers.search!.headers).toEqual(MCP_SERVERS.search.headers);
    expect(JSON.stringify(puts)).not.toContain('"GITHUB_TOKEN":"ghp_…');
  });

  test('saving other chat settings sends the MCP references back unchanged', async ({ page }) => {
    const puts: Array<{ global: { chat: { mcp_servers: unknown } } }> = [];
    await mockManager(page, {
      extra: (method, path, _url, body) => {
        if (path === '/settings/schema') return { json: SCHEMA };
        if (path === '/settings/effective') return { json: { model: null, values: {} } };
        if (path === '/settings' && method === 'PUT') {
          puts.push(body as (typeof puts)[number]);
          return { json: { restart_required: false, applied: {} } };
        }
        if (path === '/settings' && method === 'GET') return { json: { ...SETTINGS, secrets: { api_key_set: false, hf_token_override_set: false, hf_login_token_present: false }, resolved: {}, read_only: false, load_warnings: [] } };
        return undefined;
      },
    });
    await page.goto('/admin/settings/chat');
    await page.locator('[data-key="chat.default_system"] input').fill('Be brief.');
    await page.getByRole('button', { name: /^Save/ }).first().click();
    await expect.poll(() => puts.length).toBe(1);
    expect(puts[0]!.global.chat.mcp_servers).toEqual(MCP_SERVERS);
  });
});

test.describe('D44 warnings', () => {
  test('Hermes row warns that the API key is stored in plain text', async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'desktop layout');
    const integrations = { ...INTEGRATIONS, cli: INTEGRATIONS.cli.map((c) => (c.name === 'hermes' ? { ...c, plaintext_key_warning: true } : c)) };
    await mockManager(page, { engine: READY, extra: (_m, path) => (path === '/integrations' ? { json: integrations } : undefined) });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.goto('/admin/integrations');
    const hermes = page.locator('.integration-row', { has: page.getByRole('heading', { name: /Hermes/ }) });
    await expect(hermes.getByTestId('hermes-plaintext')).toContainText('stores your API key in plain text in the Hermes profile');
    await expect(page.getByTestId('hermes-plaintext')).toHaveCount(1);
  });

  test('Storage warns when the models folder is the user’s own Hugging Face cache', async ({ page }) => {
    let shared = true;
    await mockManager(page, {
      extra: (_m, path) => {
        if (path === '/settings/schema') return { json: SCHEMA };
        if (path === '/storage')
          return { json: { models_dir: '/Users/a/.cache/huggingface/hub', cache_dir: '/c', tmp_dir: '/t', splash_data_dir: '/s', models_shared_with_hf_cache: shared, hf_cache_path: '/Users/a/.cache/huggingface/hub' } };
        return undefined;
      },
    });
    await page.goto('/admin/settings/storage');
    await expect(page.getByTestId('storage-shared-hf')).toContainText('your own Hugging Face cache at /Users/a/.cache/huggingface/hub');
    shared = false;
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Models & storage.' })).toBeVisible();
    await expect(page.getByTestId('storage-shared-hf')).toHaveCount(0);
  });
});
