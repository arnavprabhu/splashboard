/**
 * Gap 10.2 step 1: in a browser, "Install Homebrew" asks the manager to open Terminal with the
 * official installer (POST /system/brew/install, system/api.py), and the wizard then polls for brew.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../src/api/client';
import { openBrewInstaller } from '../src/routes/welcome/api';

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.restoreAllMocks());

describe('Install Homebrew in a browser (SPEC §10.2, W4)', () => {
  it('posts to the manager, which opens Terminal with the official installer', async () => {
    const calls: { url: string; method: string | undefined }[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      calls.push({ url: String(input), method: init?.method });
      return json(200, { ok: true, command: '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' });
    });
    await expect(openBrewInstaller()).resolves.toBeUndefined();
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url.endsWith('/api/admin/system/brew/install')).toBe(true);
    expect(calls[0]!.method).toBe('POST');
  });

  it('409 means Homebrew is already installed, which the poll shows, so it is not an error', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(409, { error: { message: 'Homebrew is already installed', type: 'conflict_error', code: 'brew_installed' } }));
    await expect(openBrewInstaller()).resolves.toBeUndefined();
  });

  it('a Terminal that could not open reaches the caller, which shows it and keeps the copyable command', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(503, { error: { message: 'Could not open Terminal', type: 'overloaded_error', code: 'terminal_failed' } }));
    const err = await openBrewInstaller().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(503);
    expect((err as ApiError).code).toBe('terminal_failed');
  });
});
