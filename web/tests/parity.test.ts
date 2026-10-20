/**
 * Validation parity with the engine: the client-side parsers must
 * accept and reject exactly what Splash does, with the same messages. Runs Splash's
 * own parsers (the ./splash reference clone, else the Homebrew install) under the
 * bundled Python; skipped when neither is available (e.g. on CI without Splash).
 */
import { describe, expect, it } from 'vitest';
import { modelIdError } from '../src/lib/model-id';
import { parseExpertMemory, parseMaxCacheDisk, parseMaxCacheDiskWrites, parseMaxContext, parseMaxMemory, parsePleMemory, parseRequestSize, parseSparseKvMemory } from '../src/lib/size';
import { SPLASH_MESSAGES, durationSeconds, fieldError, idleReleaseError } from '../src/routes/settings/validate';

type SpawnSync = (
  cmd: string,
  args: string[],
  opts: { input: string; cwd: string; encoding: 'utf8'; timeout: number; env: Record<string, string | undefined> },
) => { status: number | null; stdout: string; stderr: string };

// Loaded through a variable so the web tsconfig needs no Node typings.
const cpName = 'node:child_process';
const fsName = 'node:fs';
const { spawnSync } = (await import(/* @vite-ignore */ cpName)) as { spawnSync: SpawnSync };
const { existsSync } = (await import(/* @vite-ignore */ fsName)) as { existsSync: (p: string) => boolean };

declare const process: { env: Record<string, string | undefined>; cwd(): string };

const BREW = '/opt/homebrew/opt/splash/libexec';
const PYTHON = process.env.SPLASH_PYTHON ?? `${BREW}/python/bin/python3`;
const SOURCE = [process.env.SPLASH_PKG, `${process.cwd()}/../splash`, BREW].find(
  (dir): dir is string => Boolean(dir) && existsSync(`${dir}/server/serve_options.py`) && existsSync(`${dir}/install/models.py`),
);
const available = existsSync(PYTHON) && SOURCE !== undefined;

const SCRIPT = `
import argparse, json, math, sys
sys.path.insert(0, ".")
from server import serve_options as s
from install import models as m
cases = json.load(sys.stdin)
out = {}
for name, values in cases.items():
    results = []
    for v in values:
        try:
            if name == "model_id":
                m.split_model_id(v); results.append({"ok": None})
            else:
                ok = getattr(s, name)(v)
                results.append({"ok": "inf" if isinstance(ok, float) and math.isinf(ok) else ok})
        except (argparse.ArgumentTypeError, m.ModelError) as e:
            results.append({"error": str(e)})
    out[name] = results
print(json.dumps(out))
`;

const SIZES = ['auto', ' AUTO ', '32G', '32g', '32GB', '32GiB', '32 G', '512M', '64K', '64kb', '1048576', '1_000', '+5G',
  '0', '00', '-1G', '1.5G', 'G', '', ' ', '32T', 'abc', '1__0', '_1', '9223372036854775807', '9223372036854775808', '8E', '5 GB ',
  // Fractions (1.4.0): only before a unit, rounded down to whole bytes.
  '2.5G', '2.5', '0.5K', '.5G', '5.G', '1.0009765625K', '1.99999999999999999999999999999G', '7.99999999999G', '1.5 G'];

const CASES: Record<string, string[]> = {
  parse_max_memory: SIZES,
  parse_max_cache_disk: [...SIZES, ' 0 '],
  parse_max_cache_disk_writes: [...SIZES, ' 0 ', '200G'],
  parse_request_size: SIZES,
  // The streamed stores (1.4.0): auto, off where the store has it, or a size.
  parse_expert_memory: [...SIZES, 'off', ' OFF ', 'none'],
  parse_ple_memory: ['auto', 'off', '2G', '512M', '0', 'x'],
  parse_sparse_kv_memory: ['auto', 'off', '1200M', '1.5G', '0', ''],
  // Durations (1.2.1+): --request-timeout and --idle-release.
  parse_request_timeout: ['3600', '2.5', '30m', '2h', '90s', '1.5h', ' 30M ', '1_0m', '1e3', '0', '-1', '0m', 'm', '', 'off', 'inf', 'nan', '1e400', '10 m', '30d', '5mm'],
  parse_idle_release: ['off', 'OFF', ' off ', '10m', '600', '90s', '2h', '0', '-5m', 'never', '', '1.5h', 'inf'],
  parse_max_context: ['auto', 'AUTO', '100K', '100k', '256K', '257K', '262144', '262145', '1', '0', '-1', '1.5K', 'K', '', ' 64 K', '1_0K',
    '512K', '1M', '1m', '986K', '987K', '1010000', '1010001', '2M', '1.5M', '+1M', 'M'],
  model_id: [
    'mlx-community/Qwen3.8-27B-4bit',
    'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL',
    'qwen27',
    'a/b/c',
    '_x/y',
    'x-/y',
    'x/y.',
    'a--b/c',
    'a/b..c',
    'a/b.git',
    `a/${'r'.repeat(96)}`,
    `a/${'r'.repeat(97)}`,
    'a/b:',
    'a/b:-x',
    `a/b:${'v'.repeat(64)}`,
    `a/b:${'v'.repeat(65)}`,
    'a/b:x..y',
    'a/b:x:y',
    ' a/b',
  ],
};

type PyResult = { ok: number | string | null } | { error: string };

function ts(name: string, value: string): PyResult {
  if (name === 'model_id') {
    const error = modelIdError(value);
    return error ? { error } : { ok: null };
  }
  if (name === 'parse_request_timeout') {
    const seconds = durationSeconds(value);
    return seconds === null ? { error: SPLASH_MESSAGES.requestTimeout } : { ok: seconds };
  }
  if (name === 'parse_idle_release') {
    const error = idleReleaseError(value);
    if (error) return { error };
    return value.trim().toLowerCase() === 'off' ? { ok: 'inf' } : { ok: durationSeconds(value) };
  }
  if (name === 'parse_max_context') {
    const r = parseMaxContext(value);
    return r.ok ? { ok: r.tokens } : { error: r.error };
  }
  const parser = {
    parse_max_memory: parseMaxMemory,
    parse_max_cache_disk: parseMaxCacheDisk,
    parse_max_cache_disk_writes: parseMaxCacheDiskWrites,
    parse_request_size: parseRequestSize,
    parse_expert_memory: parseExpertMemory,
    parse_ple_memory: parsePleMemory,
    parse_sparse_kv_memory: parseSparseKvMemory,
  }[
    name as 'parse_max_memory'
  ];
  const r = parser(value);
  // The client reads a store's off as 0 bytes; Splash returns "off".
  if (r.ok && r.bytes === 0 && name.endsWith('_memory') && name !== 'parse_max_memory') return { ok: 'off' };
  return r.ok ? { ok: r.bytes } : { error: r.error };
}

describe.skipIf(!available)(`parity with Splash's parsers (${SOURCE ?? 'unavailable'})`, () => {
  const run = spawnSync(PYTHON, ['-c', SCRIPT], {
    input: JSON.stringify(CASES),
    cwd: SOURCE ?? '.',
    encoding: 'utf8',
    timeout: 30_000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
  const expected = run.status === 0 ? (JSON.parse(run.stdout) as Record<string, PyResult[]>) : null;

  it('ran the engine parsers', () => {
    expect(run.stderr).toBe('');
    expect(expected).not.toBeNull();
  });

  for (const [name, values] of Object.entries(CASES)) {
    it(`${name} matches for ${values.length} inputs`, () => {
      const want = expected?.[name] ?? [];
      const got = values.map((v) => ts(name, v));
      const rows = values.map((v, i) => ({ input: v, splash: want[i], web: got[i] }));
      expect(rows.filter((r) => JSON.stringify(r.splash) !== JSON.stringify(r.web))).toEqual([]);
    });
  }
});

describe('Settings: Release weights when idle', () => {
  const rule = { key: 'serve.idle_release', control: 'text' };

  it('refuses a cleared field, which the manager would reject as null', () => {
    expect(fieldError(rule, null)).toBe(SPLASH_MESSAGES.idleRelease);
    expect(fieldError(rule, '  ')).toBe(SPLASH_MESSAGES.idleRelease);
  });

  it('accepts off and durations', () => {
    expect([fieldError(rule, 'off'), fieldError(rule, '30m'), fieldError(rule, '600')]).toEqual([null, null, null]);
  });
});
