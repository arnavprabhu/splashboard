/**
 * The chat workspace (2026-10-06): a full-viewport plane without the admin nav band, a wordmark
 * that returns to the admin page the tab came from, live context use and the stat tiles.
 */
import { expect, test, type Page } from '@playwright/test';

import { CHAT, V1_MODELS, mockManager } from './fixtures';

const READY = { state: 'ready', model: CHAT.model, engine: { found: true, version: '1.2.0', support: 'supported' } };

async function chatMocks(page: Page) {
  await mockManager(page, {
    engine: READY,
    extra: (method, path) => {
      if (path === '/chats' && method === 'GET') return { json: { chats: [{ id: 'c1', title: CHAT.title, created_at: CHAT.created_at, updated_at: CHAT.updated_at, model: CHAT.model, profile: 'default', message_count: 2, snippet: null }] } };
      if (path === '/chats/c1') return { json: CHAT };
      if (path.endsWith('/profiles')) return { json: { model: CHAT.model, profiles: [], sampling_defaults: {} } };
      return undefined;
    },
  });
  await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
}

test('the admin nav band is hidden on the chat and shown again on other pages', async ({ page }) => {
  await chatMocks(page);
  await page.goto('/admin/status');
  await expect(page.locator('.navband')).toBeVisible();
  await page.goto('/admin/chat/c1');
  await expect(page.getByText('Here are the action items.')).toBeVisible();
  await expect(page.locator('.navband')).toBeHidden();
  await expect(page.getByRole('navigation', { name: 'Main' })).toBeHidden();
  // The workspace fills the viewport: no page scroll.
  expect(await page.evaluate(() => document.documentElement.scrollHeight - innerHeight)).toBeLessThanOrEqual(1);
  await page.getByTestId('chat-home').click();
  await expect(page).toHaveURL(/\/admin\/status$/);
  await expect(page.locator('.navband')).toBeVisible();
});

test('the wordmark returns to the admin page this tab came from', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'the desktop nav band has the Chat link');
  await chatMocks(page);
  await page.goto('/admin/logs');
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Chat' }).click();
  await expect(page).toHaveURL(/\/admin\/chat$/);
  const home = page.getByTestId('chat-home');
  await expect(home).toHaveAttribute('href', '/admin/logs');
  await expect(home).toHaveAccessibleName('Splashboard: back to the admin');
  // Opening a conversation keeps the target: chat routes never count.
  await page.goto('/admin/chat/c1');
  await expect(page.getByTestId('chat-home')).toHaveAttribute('href', '/admin/logs');
  await page.getByTestId('chat-home').click();
  await expect(page).toHaveURL(/\/admin\/logs$/);
});

test('a deep link to the chat returns to Status, and the sidebar links reach the other screens', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'desktop sidebar');
  await page.setViewportSize({ width: 1280, height: 800 });
  await chatMocks(page);
  await page.goto('/admin/chat/c1');
  await expect(page.getByTestId('chat-home')).toHaveAttribute('href', '/admin/status');
  const links = page.getByRole('navigation', { name: 'Admin pages' });
  await expect(links.getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/admin/settings');
  await expect(links.getByRole('link', { name: 'Models' })).toHaveAttribute('href', '/admin/models');
  await expect(links.getByRole('link', { name: 'Downloads' })).toHaveAttribute('href', '/admin/models/downloader');
  // The theme toggle the hidden nav band carries is mirrored in the sidebar.
  await page.getByTestId('chat-theme-toggle').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('context used shows tokens and the percentage, with the total on hover and focus', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'hover');
  await chatMocks(page);
  await page.goto('/admin/chat/c1');
  // usage: 12,288 prompt + 431 output of a 262,144-token window.
  const meter = page.getByTestId('chat-context');
  await expect(meter).toHaveText(/^12,719 tokens · 4\.9%$/i);
  await meter.hover();
  await expect(page.getByRole('tooltip')).toHaveText('Context used: 12,719 of 262,144 tokens');
  await page.mouse.move(0, 0);
  await expect(page.getByRole('tooltip')).toBeHidden();
  await meter.focus();
  await expect(page.getByRole('tooltip')).toHaveText('Context used: 12,719 of 262,144 tokens');
});

test('the stat tiles show the last reply, then live numbers while a reply runs', async ({ page }, info) => {
  test.skip(info.project.name === 'phone', 'the panel is a column on desktop');
  await page.setViewportSize({ width: 1400, height: 900 });
  await chatMocks(page);
  const client: Array<string | undefined> = [];
  await page.route('**/v1/chat/completions', (r) => {
    client.push(r.request().headers()['x-splashboard-client']);
    return r.fulfill({
      headers: { 'content-type': 'text/event-stream' },
      body:
        [
          { choices: [{ delta: {}, index: 0 }], prompt_progress: { total: 12800, cache: 12288, processed: 12800, time_ms: 10 } },
          { choices: [{ delta: { content: 'Done.' }, index: 0, finish_reason: 'stop' }], timings: { prompt_per_second: 2048.5, predicted_per_second: 70.5, prompt_ms: 250, predicted_ms: 500 } },
          { choices: [], usage: { prompt_tokens: 12800, completion_tokens: 3, prompt_tokens_details: { cached_tokens: 12288 } } },
        ]
          .map((c) => `data: ${JSON.stringify(c)}\n\n`)
          .join('') + 'data: [DONE]\n\n',
    });
  });
  await page.goto('/admin/chat/c1');
  const tile = (id: string) => page.getByTestId(`chat-tile-${id}`);
  await expect(tile('prefill')).toContainText('—');
  await expect(tile('prefill')).toContainText('12,288 tok');
  await expect(tile('decode')).toContainText('74.2');
  await expect(tile('decode')).toContainText('431 tok');
  await expect(tile('ttft')).toContainText('0.31');
  await expect(tile('ttft')).toContainText('8,192 cached');
  await expect(tile('duration')).toContainText('—');
  await page.getByRole('textbox', { name: 'Message' }).fill('again');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText('Done.')).toBeVisible();
  expect(client).toEqual(['chat']);
  await expect(tile('prefill')).toContainText('2,049');
  await expect(tile('prefill')).toContainText('12,800 tok');
  await expect(tile('decode')).toContainText('70.5');
  await expect(tile('decode')).toContainText('3 tok');
  // Duration is measured here from send; any small positive number.
  await expect(tile('duration')).toContainText(/\d\.\d\d/);
  await expect(page.getByTestId('chat-context')).toHaveText(/^12,803 tokens · 4\.9%$/i);
});

test('phone: one column with a slim top bar, big tap targets and no horizontal scroll', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await chatMocks(page);
  await page.goto('/admin/chat/c1');
  await expect(page.getByText('Here are the action items.')).toBeVisible();
  await expect(page.locator('.chat-side')).toHaveCount(0);
  await expect(page.locator('.chat-options')).toHaveCount(0);
  for (const name of [/Show conversations/, /^Panel/]) {
    const box = (await page.getByRole('button', { name }).boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
  }
  for (const name of ['Attach', 'Send']) {
    const box = (await page.getByRole('button', { name, exact: true }).boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
  }
  const overflow = await page.evaluate(() => {
    const style = document.createElement('style');
    style.textContent = 'html,body{overflow-x:visible !important}';
    document.head.append(style);
    return document.body.scrollWidth - document.documentElement.clientWidth;
  });
  expect(overflow).toBeLessThanOrEqual(0);
  await page.getByRole('button', { name: /Show conversations/ }).click();
  const sheet = page.getByRole('dialog', { name: 'Chats.' });
  await expect(sheet.getByRole('link', { name: 'Downloads' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(sheet).toHaveCount(0);
});
