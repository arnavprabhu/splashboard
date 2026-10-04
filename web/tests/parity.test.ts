/**
 * Validation parity with the engine (SPEC §8.3, §20.2): the client-side parsers must
 * accept and reject exactly what Splash does, with the same messages. Runs Splash's
 * own parsers (the ./splash reference clone, else the Homebrew install) under the
 * bundled Python; skipped when neither is available (e.g. on CI without Splash).
 */
import { describe, expect, it } from 'vitest';
import { modelIdError } from '../src/lib/model-id';
import { parseMaxCacheDisk, parseMaxContext, parseMaxMemory, parseRequestSize } from '../src/lib/size';

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
import argparse, json, sys
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
                results.append({"ok": getattr(s, name)(v)})
        except (argparse.ArgumentTypeError, m.ModelError) as e:
            results.append({"error": str(e)})
    out[name] = results
print(json.dumps(out))
`;

const SIZES = ['auto', ' AUTO ', '32G', '32g', '32GB', '32GiB', '32 G', '512M', '64K', '64kb', '1048576', '1_000', '+5G',
  '0', '00', '-1G', '1.5G', 'G', '', ' ', '32T', 'abc', '1__0', '_1', '9223372036854775807', '9223372036854775808', '8E', '5 GB '];

const CASES: Record<string, string[]> = {
  parse_max_memory: SIZES,
  parse_max_cache_disk: [...SIZES, ' 0 '],
  parse_request_size: SIZES,
  parse_max_context: ['auto', 'AUTO', '100K', '100k', '256K', '257K', '262144', '262145', '1', '0', '-1', '1.5K', 'K', '', ' 64 K', '1_0K'],
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

type PyResult = { ok: number | null } | { error: string };

function ts(name: string, value: string): PyResult {
  if (name === 'model_id') {
    const error = modelIdError(value);
    return error ? { error } : { ok: null };
  }
  if (name === 'parse_max_context') {
    const r = parseMaxContext(value);
    return r.ok ? { ok: r.tokens } : { error: r.error };
  }
  const parser = { parse_max_memory: parseMaxMemory, parse_max_cache_disk: parseMaxCacheDisk, parse_request_size: parseRequestSize }[
    name as 'parse_max_memory'
  ];
  const r = parser(value);
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
