import { defineConfig, devices } from '@playwright/test';

declare const process: { env: Record<string, string | undefined> };

const PORT = Number(process.env.SPLASH_GUI_E2E_PORT ?? 4317);

/**
 * Runs against the production build served by `vite preview`. API calls go to the
 * manager proxy target; with nothing listening the shell renders its offline state.
 * The fake-engine flows (SPEC §20.2) are added by later tracks.
 */
export default defineConfig({
  testDir: 'e2e',
  fullyParallel: true,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'phone', use: { ...devices['Pixel 7'] } },
  ],
  webServer: {
    command: `pnpm build && pnpm exec vite preview --port ${PORT} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/admin/`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { SPLASH_GUI_MANAGER: process.env.SPLASH_GUI_MANAGER ?? 'http://127.0.0.1:9' },
  },
});
