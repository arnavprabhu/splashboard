/**
 * Real-manager fixtures for Playwright (SPEC §20.2): each test gets its own Splashboard manager
 * (`uv run --project ../manager splash-gui-manager`) on a free port, with a fresh temporary
 * SPLASH_GUI_HOME, in-memory secrets, and the fake engine (`scripts/fake_splash`) as the
 * Splash CLI. The manager serves the built SPA from web/dist, so `pnpm build` must run first
 * (`pnpm e2e` does). Nothing touches the owner's ~/.splash or port 8000.
 *
 *   import { test, expect } from './support/manager';
 *   test('…', async ({ page, manager }) => { await manager.completeWizard(); await page.goto('/admin/status'); });
 *
 * Tests that need routes the backend has not built yet use `page.route` stubs on top
 * (see ./stubs.ts) and say so in their title: "[stub: <route>]".
 */

import { test as base, expect, type BrowserContext } from '@playwright/test';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
export const WEB = resolve(here, '..', '..');
export const REPO = resolve(WEB, '..');
export const FAKE_SPLASH = join(REPO, 'scripts', 'fake_splash', 'pkg', 'bin', 'splash');
const FAKE_PYTHON = join(REPO, 'scripts', 'fake_splash', 'pkg', 'python', 'bin', 'python3');
const FAKE_INSTALLER = join(REPO, 'scripts', 'fake_splash', 'pkg', 'install', 'models.py');
/** The built SPA the manager serves; SPLASH_GUI_E2E_DIST lets parallel builds use their own outDir. */
export const DIST = process.env.SPLASH_GUI_E2E_DIST ? resolve(WEB, process.env.SPLASH_GUI_E2E_DIST) : join(WEB, 'dist');

/** A small fast GGUF selection the fake engine and installer accept. */
export const FAKE_MODEL = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M';

export interface ManagerOptions {
  /** Extra environment for the manager and everything it spawns (FAKE_SPLASH_* knobs). */
  env?: Record<string, string>;
  /** Mark the welcome wizard complete before the test starts (default true). */
  wizardCompleted?: boolean;
  /** Models to install with the fake installer before the manager starts. */
  installed?: string[];
  /**
   * Sign the test's browser context in before the test (default true). Admin sign-in is on by
   * default (D58), so pages need a session; tests of the sign-in flow itself pass false.
   */
  signedIn?: boolean;
}

export interface RealManager {
  url: string;
  port: number;
  home: string;
  token: string;
  /** Calls the admin API with the CLI token (passes CSRF and auth). */
  api<T = unknown>(method: string, path: string, body?: unknown): Promise<{ status: number; body: T }>;
  /** Reads and rewrites the settings document with `edit`. */
  patchSettings(edit: (doc: SettingsDoc) => void): Promise<void>;
  completeWizard(): Promise<void>;
  /** `POST /auth/link` with the CLI token: a one-time `/admin/login?code=…` path (D58). */
  mintLink(): Promise<string>;
  /** Gives `context` a session cookie the way the menu bar does: mint a link, exchange the code. */
  signIn(context: BrowserContext): Promise<void>;
  log(): string;
}

export interface SettingsDoc {
  version: number;
  global: Record<string, Record<string, unknown>>;
  models: Record<string, Record<string, unknown>>;
}

function freePort(): Promise<number> {
  return new Promise((ok, fail) => {
    const srv = createServer();
    srv.unref();
    srv.on('error', fail);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      srv.close(() => ok(port));
    });
  });
}

async function waitForHealth(url: string, child: ChildProcess, logs: string[], timeoutMs = 45_000): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (child.exitCode !== null) throw new Error(`manager exited with ${child.exitCode}:\n${logs.join('')}`);
    try {
      const res = await fetch(`${url}/health`);
      if (res.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`manager did not answer /health within ${timeoutMs} ms:\n${logs.join('')}`);
}

/** Installs models with the fake installer into `<home>/models`, as the manager would. */
export function fakeInstall(home: string, ids: string[], env: Record<string, string> = {}): void {
  for (const id of ids) {
    execFileSync(FAKE_PYTHON, [FAKE_INSTALLER, '--model', id, 'prepare'], {
      env: {
        ...process.env,
        HF_HUB_CACHE: join(home, 'models'),
        SPLASH_GUI_FAKE_DATA: join(home, 'fake-data'),
        FAKE_SPLASH_DL_BPS: '0',
        ...env,
      },
      stdio: 'pipe',
    });
  }
}

export async function startManager(options: ManagerOptions = {}): Promise<RealManager & { stop: () => Promise<void> }> {
  if (!existsSync(join(DIST, 'index.html'))) throw new Error(`${DIST} is missing; run \`pnpm build\` (or \`pnpm e2e\`).`);
  const port = await freePort();
  const root = mkdtempSync(join(tmpdir(), 'splash-gui-e2e-'));
  const home = join(root, 'home');
  const env: Record<string, string> = {
    ...(process.env as Record<string, string>),
    SPLASH_GUI_HOME: home,
    SPLASH_GUI_SECRETS: 'memory',
    SPLASH_GUI_REAL_SPLASH: FAKE_SPLASH,
    SPLASH_GUI_FAKE_DATA: join(home, 'fake-data'),
    HF_HUB_CACHE: join(home, 'models'),
    FAKE_SPLASH_LOAD_SECONDS: '0.2',
    FAKE_SPLASH_TOKS: '400',
    FAKE_SPLASH_DL_BPS: '50000000',
    PYTHONUNBUFFERED: '1',
    ...options.env,
  };
  delete env.SPLASH_API_KEY;
  if (options.installed?.length) fakeInstall(home, options.installed, options.env);
  const logs: string[] = [];
  const child = spawn(
    'uv',
    ['run', '--project', join(REPO, 'manager'), 'splash-gui-manager', '--port', String(port), '--web-dist', DIST, '--console', '--log-level', 'info'],
    { env, stdio: ['ignore', 'pipe', 'pipe'], detached: true },
  );
  child.stdout?.on('data', (d: Buffer) => logs.push(d.toString()));
  child.stderr?.on('data', (d: Buffer) => logs.push(d.toString()));
  const url = `http://127.0.0.1:${port}`;
  const stop = async () => {
    if (child.exitCode === null && child.pid) {
      try {
        process.kill(-child.pid, 'SIGTERM');
      } catch {
        /* already gone */
      }
      await new Promise<void>((r) => {
        const timer = setTimeout(() => {
          try {
            if (child.pid) process.kill(-child.pid, 'SIGKILL');
          } catch {
            /* gone */
          }
          r();
        }, 5000);
        child.once('exit', () => {
          clearTimeout(timer);
          r();
        });
      });
    }
    rmSync(root, { recursive: true, force: true });
  };
  try {
    await waitForHealth(url, child, logs);
  } catch (err) {
    await stop();
    throw err;
  }
  const token = readFileSync(join(home, 'run', 'cli.token'), 'utf8').trim();
  const api = async <T,>(method: string, path: string, body?: unknown) => {
    const res = await fetch(`${url}/api/admin${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await res.text();
    let parsed: unknown = text;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      /* keep text */
    }
    return { status: res.status, body: parsed as T };
  };
  const patchSettings = async (edit: (doc: SettingsDoc) => void) => {
    const got = await api<{ settings: SettingsDoc }>('GET', '/settings');
    const doc = got.body.settings;
    edit(doc);
    const put = await api('PUT', '/settings', doc);
    if (put.status !== 200) throw new Error(`PUT /settings → ${put.status}: ${JSON.stringify(put.body)}`);
  };
  const mintLink = async () => {
    const res = await api<{ url: string }>('POST', '/auth/link');
    if (res.status !== 200) throw new Error(`POST /auth/link → ${res.status}: ${JSON.stringify(res.body)}`);
    return res.body.url;
  };
  const signIn = async (context: BrowserContext) => {
    const code = new URL(await mintLink(), url).searchParams.get('code');
    // context.request shares the context's cookie jar; the exchange wants a same-origin page.
    const res = await context.request.post(`${url}/api/admin/auth/exchange`, {
      data: { code },
      headers: { Origin: url, 'Sec-Fetch-Site': 'same-origin' },
    });
    if (!res.ok()) throw new Error(`POST /auth/exchange → ${res.status()}: ${await res.text()}`);
  };
  const manager = {
    url,
    port,
    home,
    token,
    api,
    mintLink,
    signIn,
    patchSettings,
    completeWizard: () =>
      patchSettings((doc) => {
        doc.global.wizard = { ...(doc.global.wizard ?? {}), completed: true };
      }),
    log: () => logs.join(''),
    stop,
  };
  if (options.wizardCompleted !== false) await manager.completeWizard();
  return manager;
}

/** Signs a browser context in, or null to leave it signed out. */
export type SignInBrowser = ((context: BrowserContext) => Promise<void>) | null;

interface Fixtures {
  managerOptions: ManagerOptions;
  manager: RealManager;
  /**
   * How `context` gets its session (D58). Harnesses that bring their own manager (the
   * verify skill's specs/support.ts) override this so the per-test manager is never started.
   */
  signInBrowser: SignInBrowser;
}

export const test = base.extend<Fixtures>({
  managerOptions: [{}, { option: true }],
  manager: async ({ managerOptions }, use, testInfo) => {
    const m = await startManager(managerOptions);
    try {
      await use(m);
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) await testInfo.attach('manager.log', { body: m.log(), contentType: 'text/plain' });
      await m.stop();
    }
  },
  baseURL: async ({ manager }, use) => {
    await use(manager.url);
  },
  signInBrowser: async ({ manager, managerOptions }, use) => {
    await use(managerOptions.signedIn === false ? null : (context) => manager.signIn(context));
  },
  context: async ({ context, signInBrowser }, use) => {
    if (signInBrowser) await signInBrowser(context);
    await use(context);
  },
});

export { expect };
