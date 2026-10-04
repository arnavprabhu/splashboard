import { defineConfig, devices } from '@playwright/test';

declare const process: { env: Record<string, string | undefined> };

const PORT = Number(process.env.SPLASH_GUI_E2E_PORT ?? 4317);

/**
 * Two kinds of tests (SPEC §20.2):
 * - e2e/real/**: each test starts its own real manager with the fake engine
 *   (e2e/support/manager.ts); the manager serves web/dist. Missing backend routes are
 *   stubbed per test with e2e/support/stubs.ts and marked "[stub: …]" in the title.
 * - e2e/*.spec.ts: shell smoke tests against `vite preview` with every admin call
 *   stubbed by page.route (layout, phone width, axe, theme).
 * Run `pnpm e2e` (builds first). `pnpm exec playwright test --project=real` runs one kind.
 */
export default defineConfig({
  testDir: 'e2e',
  fullyParallel: true,
  workers: process.env.CI ? 2 : undefined,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 60_000,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', testIgnore: /real\//, use: { ...devices['Desktop Chrome'] } },
    { name: 'phone', testIgnore: /real\//, use: { ...devices['Pixel 7'] } },
    { name: 'real', testMatch: /real\/.*\.spec\.ts/, use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: `pnpm exec vite preview --port ${PORT} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/admin/`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { SPLASH_GUI_MANAGER: process.env.SPLASH_GUI_MANAGER ?? 'http://127.0.0.1:9' },
  },
});
