/**
 * D59 (Q16) against the real manager, the fake engine and the fake Hub: the compatibility
 * check shows the likely recommended variant's verdict first, then fills in the others.
 * `FAKE_SPLASH_INSPECT_SECONDS` makes every other header read take 2 s, so the first verdict
 * must be on screen while the rest still say "Checking…".
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { join } from 'node:path';

import { expect, REPO, startManager, test } from '../support/manager';

const FAKE_PYTHON = join(REPO, 'scripts', 'fake_splash', 'pkg', 'python', 'bin', 'python3');
const GGUF = 'unsloth/Qwen3.8-27B-GGUF';

async function startHub(): Promise<{ url: string; child: ChildProcess }> {
  const child = spawn(FAKE_PYTHON, [join(REPO, 'scripts', 'fake_splash', 'hub.py')], { stdio: ['ignore', 'pipe', 'pipe'] });
  const url = await new Promise<string>((ok, fail) => {
    let out = '';
    child.stdout!.on('data', (d: Buffer) => {
      out += d.toString();
      const line = out.split('\n').find((l) => l.startsWith('http'));
      if (line) ok(line.trim());
    });
    child.once('exit', (code) => fail(new Error(`fake Hub exited with ${code}`)));
  });
  return { url, child };
}

test('By ID: the recommended variant’s verdict shows first, the others fill in', async ({ page }) => {
  test.setTimeout(90_000);
  const hub = await startHub();
  const manager = await startManager({ env: { HF_ENDPOINT: hub.url, FAKE_SPLASH_INSPECT_SECONDS: '2,UD-Q4_K_M=0.5' } });
  try {
    await manager.patchSettings((doc) => {
      doc.global.hf = { ...(doc.global.hf ?? {}), endpoint: hub.url };
    });
    await manager.signIn(page.context()); // D58: sign-in is on; a one-time link for this manager
    await page.setViewportSize({ width: 1280, height: 900 });
    const streamed = page.waitForRequest((r) => r.url().includes('/api/admin/inspect/stream'));
    await page.goto(`${manager.url}/admin/models/downloader?tab=id&id=${GGUF}`);
    expect((await streamed).method()).toBe('POST'); // D58: a read-only POST, like /inspect

    const table = page.getByTestId('variant-table');
    const row = (name: string) => table.getByRole('row').filter({ has: page.getByText(name, { exact: true }) });
    // First verdict: UD-Q4_K_M is checked and recommended; the others are still being checked.
    await expect(row('UD-Q4_K_M')).toContainText('Recommended');
    await expect(row('Q8_0')).toContainText('Checking…');
    await expect(page.getByTestId('inspect-progress')).toHaveText('1 of 8 variants checked. The rest are still being checked.');
    await expect(page.getByText('Compatible', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('radio', { name: 'UD-Q4_K_M' })).toBeChecked();

    // Then every row has its verdict: UD-Q8_K_XL and the imatrix file refused, no "Checking…".
    await expect(table.getByTestId('variant-checking')).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId('inspect-progress')).toHaveCount(0);
    await expect(row('UD-Q8_K_XL')).toContainText('Unsupported');
    await expect(row('imatrix_unsloth')).toContainText('Unsupported');
    await expect(row('Q8_0')).not.toContainText('Checking');
    await expect(page.getByRole('radio', { name: 'UD-Q4_K_M' })).toBeChecked();
    await expect(page.getByRole('button', { name: /^Download/ })).toBeEnabled();
  } finally {
    await manager.stop();
    hub.child.kill();
  }
});

test('detail sheet: the variant table fills in, the likely pick first', async ({ page }) => {
  test.setTimeout(90_000);
  const hub = await startHub();
  const manager = await startManager({ env: { HF_ENDPOINT: hub.url, FAKE_SPLASH_INSPECT_SECONDS: '2,UD-Q4_K_M=0.5' } });
  try {
    await manager.patchSettings((doc) => {
      doc.global.hf = { ...(doc.global.hf ?? {}), endpoint: hub.url };
    });
    await manager.signIn(page.context()); // D58: sign-in is on; a one-time link for this manager
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`${manager.url}/admin/models/downloader?model=${GGUF}`);
    const sheet = page.getByTestId('model-drawer');
    const table = sheet.getByTestId('variant-table');
    const row = (name: string) => table.getByRole('row').filter({ has: page.getByText(name, { exact: true }) });
    await expect(row('UD-Q4_K_M')).toContainText('Recommended');
    await expect(row('Q4_0')).toContainText('Checking…');
    await expect(sheet.getByTestId('inspect-progress')).toBeVisible();
    await expect(sheet.getByTestId('drawer-download')).toHaveText(/UD-Q4_K_M/);
    await expect(sheet.getByTestId('drawer-download')).toBeEnabled();
    await expect(table.getByTestId('variant-checking')).toHaveCount(0, { timeout: 30_000 });
    await expect(row('UD-Q8_K_XL')).toContainText('Unsupported');
    await expect(row('Q4_0').getByRole('button', { name: 'Download Q4_0' })).toBeVisible();
  } finally {
    await manager.stop();
    hub.child.kill();
  }
});
