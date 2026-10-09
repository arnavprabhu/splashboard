/**
 * Welcome wizard step 1 against stubbed admin routes:
 * the stop state for a Mac Splash cannot run, Check again once the macOS update is in, and
 * "Install Homebrew" in a browser, which asks the manager to open Terminal.
 */
import { expect, test } from '@playwright/test';

import { mockManager } from './fixtures';

const GB = 1_000_000_000;
const VOL = { path: '/Users/arnav/.splash/models', total_bytes: 1000 * GB, free_bytes: 300 * GB };
const SYSTEM = { chip: 'Apple M5 Pro', memory_bytes: 68_719_476_736, macos_version: '27.0', arch: 'arm64', hostname: 'mac', supported: true, unsupported_reasons: [], disk: { models: VOL, cache: VOL }, power: { source: 'ac' } };
const M2 = { ...SYSTEM, chip: 'Apple M2 Pro', supported: false, unsupported_reasons: ['Splash needs an M3 or newer; this Mac has an Apple M2 Pro'] };
const OLD_MACOS = { ...SYSTEM, macos_version: '26.1', supported: false, unsupported_reasons: ['Splash needs macOS 26.4 or newer; this Mac runs 26.1'] };
const DISCOVERY = { found: true, version: '1.3.0', support: 'supported', supported_range: '>=1.3.0,<1.4.0', source_checkout: false };
const BREW_MISSING = { installed: false };
const BREW_OK = { installed: true, path: '/opt/homebrew/bin/brew', version: '5.1.2', splash_formula_installed: true };

test('[stub: /system] an unsupported Mac gets the stop state in place of the steps', async ({ page }) => {
  await mockManager(page, { extra: (_m, path) => (path === '/system' ? { json: M2 } : undefined) });
  await page.goto('/admin/welcome?step=1');
  await expect(page.getByRole('heading', { name: 'This Mac can’t run Splash.' })).toBeVisible();
  await expect(page.getByText('Splash needs an Apple M3 or newer. This Mac has Apple M2 Pro.')).toBeVisible();
  await expect(page.getByTestId('stop-requirements')).toHaveAttribute('href', 'https://github.com/incoai/splash#quick-start');
  // No rail, no step, no Continue: the wizard cannot be walked past the stop.
  await expect(page.getByTestId('check-mac')).toHaveCount(0);
  await expect(page.getByTestId('step-meta')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Continue' })).toHaveCount(0);
});

test('[stub: /system] an old macOS offers Software Update, and Check again continues once it is updated', async ({ page }) => {
  let current = OLD_MACOS;
  await mockManager(page, {
    extra: (_m, path) => {
      if (path === '/system') return { json: current };
      if (path === '/system/brew') return { json: BREW_OK };
      if (path === '/versions') return { json: { gui: '0.1.0', manager: '0.1.0', python: '3.12', engine: DISCOVERY, engine_update: { available: false } } };
      if (path === '/doctor') return { json: { ok: true, checks: [] } };
      return undefined;
    },
  });
  await page.goto('/admin/welcome?step=1');
  await expect(page.getByRole('heading', { name: 'macOS is too old.' })).toBeVisible();
  await expect(page.getByText('Splash needs macOS 26.4 or later. This Mac runs 26.1.')).toBeVisible();
  await expect(page.getByTestId('stop-software-update')).toHaveAttribute('href', 'x-apple.systempreferences:com.apple.Software-Update-Settings.extension');
  current = SYSTEM;
  await page.getByRole('button', { name: 'Check again' }).click();
  await expect(page.getByTestId('stop-state')).toHaveCount(0);
  await expect(page.getByTestId('check-mac')).toHaveAttribute('data-status', 'ok');
});

test('[stub: /system/brew/install] in a browser, Install Homebrew asks the manager to open Terminal, then waits for brew', async ({ page }) => {
  let brew = BREW_MISSING;
  const calls = await mockManager(page, {
    extra: (method, path) => {
      if (path === '/system') return { json: SYSTEM };
      if (path === '/system/brew') return { json: brew };
      if (path === '/system/brew/install' && method === 'POST') return { json: { ok: true, command: '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' } };
      if (path === '/versions') return { json: { gui: '0.1.0', manager: '0.1.0', python: '3.12', engine: { found: false, support: 'unknown' }, engine_update: { available: false } } };
      if (path === '/doctor') return { json: { ok: true, checks: [] } };
      return undefined;
    },
  });
  await page.goto('/admin/welcome?step=1');
  await page.getByTestId('install-brew').click();
  await expect.poll(() => calls).toContain('POST /system/brew/install');
  await expect(page.getByText('Waiting for Homebrew installation…')).toBeVisible();
  // The manager answers the poll once Homebrew is installed; the row turns ok without a reload.
  brew = BREW_OK;
  await expect(page.getByTestId('check-brew')).toHaveAttribute('data-status', 'ok', { timeout: 10_000 });
});

test('[stub: /system/brew/install] a Terminal that will not open says so, and the command stays copyable', async ({ page }) => {
  await mockManager(page, {
    extra: (method, path) => {
      if (path === '/system') return { json: SYSTEM };
      if (path === '/system/brew') return { json: BREW_MISSING };
      if (path === '/system/brew/install' && method === 'POST') return { status: 503, json: { error: { message: 'Could not open Terminal', type: 'overloaded_error', code: 'terminal_failed' } } };
      if (path === '/versions') return { json: { gui: '0.1.0', manager: '0.1.0', python: '3.12', engine: { found: false, support: 'unknown' }, engine_update: { available: false } } };
      if (path === '/doctor') return { json: { ok: true, checks: [] } };
      return undefined;
    },
  });
  await page.goto('/admin/welcome?step=1');
  await page.getByTestId('install-brew').click();
  await expect(page.getByText('Could not open Terminal.', { exact: false }).first()).toBeVisible();
  await expect(page.getByTestId('brew-open-again')).toHaveCount(0);
  await expect(page.getByText('Install Homebrew in Terminal')).toBeVisible();
});
