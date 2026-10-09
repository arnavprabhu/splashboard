/**
 * UI QA pass 2026-10-04, batch 2.
 * Each test fails on the code before the fix.
 */
import { describe, expect, it } from 'vitest';
import type { DoctorReport, InstalledModel, LiveMetrics, UsageSummary } from '../src/api/models';
import { buildOptions } from '../src/components/Chart';
import { nearBottom } from '../src/routes/chat';
import { searchFormat } from '../src/routes/models-downloader';
import { defs } from '../src/routes/status/Charts';
import { sessionStats, usageStats } from '../src/routes/status/Numbers';
import { defaultInstalled, doctorCheck, installedChoices, savedPreset, servingModel, shellStatus } from '../src/routes/welcome/logic';

const report = (checks: DoctorReport['checks']): DoctorReport => ({ ok: true, checks });

describe('row 1: wizard step 1 shell command', () => {
  const path = { id: 'path', label: 'CLI PATH', status: 'warn' as const, message: 'A splash function or alias hides the shim: /Users/a/.zshrc', fix: 'Remove the splash function or alias' };
  it('reads the manager’s `path` check (system/api.py doctor)', () => {
    const r = report([path]);
    const check = doctorCheck(r, 'path') ?? doctorCheck(r, 'path_shadowing');
    expect(check?.message).toContain('.zshrc');
    expect(shellStatus(r, check)).toBe('warn');
  });
  it('a missing check is unknown (skip), never ✓', () => {
    expect(shellStatus(report([]), null)).toBe('skip');
    expect(shellStatus('unavailable', null)).toBe('skip');
    expect(shellStatus(null, null)).toBe('pending');
  });
});

describe('row 5: step 3 starts from the saved preset', () => {
  it('reads global.wizard.preset', () => {
    expect(savedPreset({ wizard: { completed: true, preset: 'coding' } })).toBe('coding');
    expect(savedPreset({ wizard: { completed: true } })).toBeNull();
    expect(savedPreset({ wizard: { preset: 'nonsense' } })).toBeNull();
    expect(savedPreset(undefined)).toBeNull();
  });
});

describe('row 6: step 4 lists installed models', () => {
  const m = (id: string, last: string | null, status: InstalledModel['status'] = 'ready') =>
    ({ id, format: 'gguf', size_bytes: 13e9, last_used_at: last, status }) as Pick<InstalledModel, 'id' | 'format' | 'size_bytes' | 'last_used_at' | 'status'>;
  const Q2 = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q2_K_XL';
  const list = [m('a/old', '2026-09-01T00:00:00Z'), m(Q2, '2026-10-03T00:00:00Z'), m('a/broken', '2026-10-04T00:00:00Z', 'broken'), m('a/rec', null)];
  it('active first, then most recently used; broken and recommended rows left out', () => {
    expect(installedChoices(list, ['a/rec'], null).map((x) => x.id)).toEqual([Q2, 'a/old']);
    expect(installedChoices(list, ['a/rec'], 'a/old').map((x) => x.id)).toEqual(['a/old', Q2]);
  });
  it('only usable installs: not downloading, paused, verifying or broken', () => {
    const busy = [m('a/dl', '2026-10-05T00:00:00Z', 'downloading'), m('a/p', '2026-10-05T00:00:00Z', 'paused'), m('a/v', '2026-10-05T00:00:00Z', 'verifying'), m('a/ok', null, 'update_available')];
    expect(installedChoices(busy, [], null).map((x) => x.id)).toEqual(['a/ok']);
    expect(defaultInstalled(busy, null)).toBe('a/ok');
  });
  it('the active model counts only while the engine serves it', () => {
    expect(servingModel({ state: 'ready', model: 'a/old' })).toBe('a/old');
    expect(servingModel({ state: 'failed', model: 'a/old' })).toBeNull();
    expect(servingModel({ state: 'stopped', model: 'a/old' })).toBeNull();
  });
  it('preselects the active model, else the most recently used', () => {
    expect(defaultInstalled(list, null)).toBe(Q2);
    expect(defaultInstalled(list, 'a/old')).toBe('a/old');
    expect(defaultInstalled([], null)).toBeNull();
  });
});

describe('row 8b: chart axes', () => {
  it('the cache hit rate chart has a fixed 0–1 range (no "10000%" when empty)', () => {
    const hit = defs().find((d) => d.id === 'hit')!;
    expect(hit.yRange).toEqual([0, 1]);
    const el = document.createElement('div');
    const opts = buildOptions(el, { data: [[]], series: [{ label: 'x' }], yRange: [0, 1] }, 400);
    expect(opts.scales?.y?.range).toEqual([0, 1]);
  });
  it('the y axis sizes itself from its labels', () => {
    const opts = buildOptions(document.createElement('div'), { data: [[]], series: [{ label: 'x' }] }, 400);
    expect(typeof opts.axes?.[1]?.size).toBe('function');
  });
});

describe('row 12: Numbers with no requests show —', () => {
  it('live: TTFT and draft acceptance are — before anything ran', () => {
    const s = { totals: { requests_completed: 0 }, latency: { ttft_p50_ms: 0, ttft_p95_ms: 0 }, draft: { acceptance_rate: 0, drafted_tokens: 0, accepted_tokens: 0 } } as unknown as LiveMetrics;
    const stats = sessionStats(s, null);
    expect(stats.find((x) => x.key === 'ttft')?.value).toBe('—');
    expect(stats.find((x) => x.key === 'draft')?.value).toBe('—');
  });
  it('live: real values still show', () => {
    const s = { totals: { requests_completed: 3 }, latency: { ttft_p50_ms: 184, ttft_p95_ms: 600 }, draft: { acceptance_rate: 0.5, drafted_tokens: 10, accepted_tokens: 5 } } as unknown as LiveMetrics;
    const stats = sessionStats(s, null);
    expect(stats.find((x) => x.key === 'ttft')?.value).not.toBe('—');
    expect(stats.find((x) => x.key === 'draft')?.value).toBe('50%');
  });
  it('usage: TTFT is — with no completed requests', () => {
    const u = { completed: 0, ttft_p50_ms: 0, ttft_p95_ms: 0 } as unknown as UsageSummary;
    expect(usageStats(u).find((x) => x.key === 'ttft')?.value).toBe('—');
  });
});

describe('row 16: search hits claim 4-bit only once checked', () => {
  const id = 'lmstudio-community/Qwen3.6-35B-A3B-MLX-8bit';
  it('unchecked MLX is just MLX', () => {
    expect(searchFormat('mlx', id, 'unchecked')).toBe('MLX');
    expect(searchFormat('mlx', id, 'incompatible')).toBe('MLX');
  });
  it('compatible MLX is MLX 4-bit', () => {
    expect(searchFormat('mlx', id, 'compatible')).toBe('MLX 4-bit');
    expect(searchFormat('unknown', id, 'compatible')).toBe('—');
  });
});

describe('row 17: the thread follows output only near the bottom', () => {
  it('within 48 px follows; scrolled up does not', () => {
    expect(nearBottom({ scrollHeight: 2000, scrollTop: 1500, clientHeight: 480 })).toBe(true);
    expect(nearBottom({ scrollHeight: 2000, scrollTop: 900, clientHeight: 480 })).toBe(false);
  });
});
