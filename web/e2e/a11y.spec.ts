import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

import { CHAT, INTEGRATIONS, USAGE, V1_MODELS, mockManager } from './fixtures';


/** axe on the pages this session rebuilt, light theme, populated data. */
for (const path of ['/admin/status/history', '/admin/integrations', '/admin/settings/data', '/admin/settings/about', '/admin/chat/c1', '/admin/tools/playground', '/admin/tools/judgments', '/admin/tools/tokenizer', '/admin/tools/benchmark', '/admin/models/downloader', '/admin/logs/diagnostics']) {
  test(`axe passes on ${path}`, async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'desktop run is enough for axe');
    await mockManager(page, {
      usage: USAGE,
      extra: (method, p) => {
        if (p === '/integrations') return { json: INTEGRATIONS };
        if (p === '/chats' && method === 'GET') return { json: { chats: [] } };
        if (p === '/chats/c1') return { json: CHAT };
        if (p === '/data/sizes') return { json: { targets: [{ target: 'chats', label: 'Chat history', bytes: 1000, items: 2, note: null }] } };
        if (p === '/benchmark/runs') return { json: { runs: [] } };
        if (p === '/benchmark/preflight') return { json: { ready: true, warnings: [] } };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await page.waitForLoadState('networkidle');
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(' ')).slice(0, 3).join(' | ')})`)).toEqual([]);
  });
}

/** No radius, shadow or gradient on the rebuilt pages, in both themes. */
for (const theme of ['light', 'dark'] as const) {
  test(`no radius, shadow or gradient on rebuilt pages (${theme})`, async ({ page }, info) => {
    test.skip(info.project.name === 'phone', 'desktop run is enough');
    await mockManager(page, {
      usage: USAGE,
      extra: (method, p) => {
        if (p === '/integrations') return { json: INTEGRATIONS };
        if (p === '/chats' && method === 'GET') return { json: { chats: [] } };
        if (p === '/chats/c1') return { json: CHAT };
        return undefined;
      },
    });
    await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
    await page.addInitScript((th) => localStorage.setItem('splash-gui-theme', th), theme);
    for (const path of ['/admin/status/history', '/admin/integrations', '/admin/chat/c1', '/admin/settings/about', '/admin/tools/playground', '/admin/tools/judgments', '/admin/models/downloader']) {
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      const bad = await page.evaluate(() => {
        const out: string[] = [];
        for (const el of Array.from(document.querySelectorAll('*'))) {
          for (const pseudo of [null, '::before', '::after']) {
            const s = getComputedStyle(el, pseudo);
            const radius = [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomLeftRadius, s.borderBottomRightRadius].some((r) => r !== '0px');
            if (radius || (s.boxShadow !== 'none' && s.boxShadow !== '') || s.backgroundImage.includes('gradient')) out.push(`${el.tagName.toLowerCase()}.${el.className}${pseudo ?? ''}`);
          }
        }
        return out;
      });
      expect(bad, `${path} (${theme})`).toEqual([]);
    }
  });
}
