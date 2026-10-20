/**
 * What Splash 1.4.0's `/status` and request errors add on screen: the SSD streaming band (routed
 * experts, the PLE table, a long request's KV on the SSD), the KV column of Active requests, the
 * header's YaRN factor, context bound and verified rows, the Cache band's notes for a spill target,
 * whole millions of tokens as "M", and Chat's SSD cache errors.
 */
import { render, screen } from '@testing-library/preact';
import { beforeEach, describe, expect, it } from 'vitest';
import { ApiError } from '../src/api/client';
import type { LiveMetrics } from '../src/api/models';
import { formatTokens } from '../src/lib/format';
import { autoRetry, classifyError, maxAutoRetries, retryClock } from '../src/routes/chat/logic';
import { ActiveRequestsBand, CacheBand, StreamingBand } from '../src/routes/status/Bands';
import { applyPatch, kvModeOf, metaKeys, showsKvMode, spillTarget, streamingOf } from '../src/routes/status/logic';
import type { SettingsDoc } from '../src/api/types';
import { settings } from '../src/store';
import { liveSample } from '../src/store/live';

const GiB = 1024 ** 3;

const latency = { p50: 0.1, p95: 0.2, p99: 0.3, max: 0.4, total: 1, samples: 10 };
const experts = (failure = '') => ({
  arena_bytes: 16 * GiB,
  staging_bytes: 400 * 1024 ** 2,
  slots: 1820,
  experts: 10240,
  resident: 1820,
  prefill: { loads: 4, routed: 100, read: 10, prefetched: 5, read_ahead: 0, hit_rate: 0.9, read_bytes: GiB, host_steps: {} },
  decode: { loads: 40, routed: 430, read: 43, prefetched: 20, read_ahead: 0, hit_rate: 0.9, read_bytes: GiB, host_steps: {} },
  failure,
});
const table = (poisoned = false) => ({
  table: 'per_layer_token_embd.weight',
  table_bytes: 28.8e9,
  cache_bytes: 512 * 1024 ** 2,
  decode: { hit_rate: 0.95, stage_ms: latency },
  read_bytes: 3 * GiB,
  read_errors: poisoned ? 1 : 0,
  poisoned,
  failure: poisoned ? 'read of the PLE table failed' : '',
});
const spill = (failures = 0) => ({
  requests: { spill: 2, resident: 5, converted: 0, bounded: 1, length_ends: 1, refused: { quota: 1, write_budget: 3, closed: 0 }, waiting_quota: 0 },
  pages: { index: 10, index_bytes: 240 * 1024, spilled: 10, spilled_bytes: 4 * GiB },
  write_through: { pages: 10, bytes: 4 * GiB, in_flight: 0, failures, paused: false },
  quota: { active_limit_bytes: 100 * GiB, held_bytes: 4 * GiB, committed_bytes: 4 * GiB, revoked: 0 },
  block_cache: { bytes: 390 * 1024 ** 2, hit_rate: 0.9375, read_failures: 0 },
  history_stream: null,
});

beforeEach(() => {
  settings.value = { settings: { version: 1, global: {}, models: {} } } as never;
  liveSample.value = null;
});

describe('SSD streaming band', () => {
  it('is hidden when nothing streams', () => {
    expect(streamingOf({ streamed_experts: null, streamed_table: null })).toBeNull();
    const { container } = render(<StreamingBand raw={{ streamed_experts: null, streamed_table: null }} stopped={false} />);
    expect(container.textContent).toBe('');
  });

  it('shows the experts, the PLE table and KV on the SSD without accent while healthy', () => {
    const raw = { streamed_experts: experts(), streamed_table: table(), kv_spill: spill() };
    expect(streamingOf(raw)).not.toBeNull();
    const { container } = render(<StreamingBand raw={raw} stopped={false} />);
    expect(screen.getByText('SSD streaming')).toBeTruthy();
    expect(container.textContent).toContain('1,820 resident in 1,820 slots of 10,240');
    expect(container.textContent).toContain('decode hit rate 90');
    expect(container.textContent).toContain('requests 2 spilled · 5 in memory');
    expect(container.textContent).toContain('refused 1 quota · 3 write budget · 0 closed · 1 ended at the bound');
    expect(container.querySelector('[data-accent="true"]')).toBeNull();
  });

  it('puts a failure in the accent', () => {
    render(<StreamingBand raw={{ streamed_experts: experts('cannot read expert 3'), streamed_table: table(true), kv_spill: spill(2) }} stopped={false} />);
    expect(screen.getByText('failed: cannot read expert 3').getAttribute('data-accent')).toBe('true');
    expect(screen.getByText('failed: read of the PLE table failed').getAttribute('data-accent')).toBe('true');
    expect(screen.getByTestId('spill-failures').textContent).toBe('2 write failures · 0 read failures');
  });

  it('collapses while the engine is stopped', () => {
    const { container } = render(<StreamingBand raw={{ streamed_experts: experts() }} stopped />);
    expect(container.textContent).toBe('');
  });
});

describe('Active requests KV column', () => {
  const row = { phase: 'decode', priority: 'normal', prompt_tokens: 600_000, prompt_processed: 600_000, generated_tokens: 3, max_new_tokens: 100, age_ms: 10 };

  it('appears only when some request reports kv_mode', () => {
    expect(kvModeOf({ kv_mode: 'spill' })).toBe('spill');
    expect(kvModeOf({ kv_mode: 'elsewhere' })).toBeNull();
    expect(showsKvMode([{}, { kv_mode: null }])).toBe(false);
    liveSample.value = { active_requests: [{ ...row, id: 1, kv_mode: 'spill' }, { ...row, id: 2, kv_mode: 'resident' }] } as unknown as LiveMetrics;
    render(<ActiveRequestsBand raw={null} stopped={false} />);
    expect(screen.getByText('KV')).toBeTruthy();
    expect(screen.getByText('SSD')).toBeTruthy();
    expect(screen.getByText('RAM')).toBeTruthy();
  });

  it('is left out for a target that keeps its KV in memory', () => {
    liveSample.value = { active_requests: [{ ...row, id: 1 }] } as unknown as LiveMetrics;
    render(<ActiveRequestsBand raw={null} stopped={false} />);
    expect(screen.queryByText('KV')).toBeNull();
  });
});

describe('Cache band on a spill target', () => {
  const disk = { capacity_bytes: 32 * GiB, limit_bytes: 32 * GiB, used_bytes: GiB, writes_gated: 'budget', write_budget_bytes: 0 };

  it('says a spent write budget refuses long requests', () => {
    expect(spillTarget({ kv_spill: spill() })).toBe(true);
    render(<CacheBand raw={{ disk, kv_spill: spill() }} stopped={false} />);
    expect(screen.getByTestId('ssd-writes').textContent).toBe('write budget spent · reusable states wait for it · long requests are refused until it refills');
  });

  it('keeps the plain line elsewhere and shows writes held back for reads', () => {
    const { container } = render(<CacheBand raw={{ disk: { ...disk, writes_deferred_ms: 1250 } }} stopped={false} />);
    expect(screen.getByTestId('ssd-writes').textContent).toBe('write budget spent · reusable states wait for it');
    expect(container.textContent).toContain('writes held 1.25 s for reads');
  });
});

describe('header meta', () => {
  it('adds YaRN, a memory or disk bound and the verified rows', () => {
    expect(metaKeys({ context: 1_010_000, yarn: 3.85, bound: 'disk', kv: 'int8', draft: null, verify: 'all' })).toEqual([
      'context',
      'yarn',
      'bound',
      'kv',
      'draft',
      'verify',
    ]);
    expect(metaKeys({ context: 262144, yarn: null, bound: 'model', verify: null })).toEqual(['context']);
  });
});

describe('token counts', () => {
  it('writes whole millions as M, as Splash reads them', () => {
    expect(formatTokens(1_000_000)).toBe('1M');
    expect(formatTokens(1_010_000)).toBe('1,010,000');
    expect(formatTokens(262144)).toBe('256K');
  });
});

describe('Chat SSD cache errors', () => {
  const error = (status: number, code: string, message: string, retry: number | null = null) =>
    classifyError(new ApiError(status, { message, type: 't', code }, null, retry));

  it('names a spent write budget with the time it refills, without retrying by itself', () => {
    const now = Date.UTC(2026, 9, 20, 12, 0);
    expect(retryClock(3600, now)).toBe(new Date(now + 3_600_000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
    const e = error(503, 'kv_write_budget', 'requests past 1000 tokens keep their KV on the SSD cache, whose daily write budget is spent', 3600);
    expect(e.kind).toBe('kv_write_budget');
    expect(e.body).toContain('until the budget refills, around');
    expect(autoRetry(e.kind)).toBe(false);
  });

  it('classifies a quota refusal, an unreadable KV and the SSD causes of capacity and timeout', () => {
    expect(error(400, 'kv_disk_quota', "this request's KV needs needed_bytes=1").kind).toBe('kv_disk_quota');
    const unreadable = error(503, 'kv_unreadable', 'KV of an earlier part of the conversation could not be read from the SSD cache', 1);
    expect(unreadable.kind).toBe('kv_unreadable');
    expect(autoRetry(unreadable.kind) && maxAutoRetries(unreadable.kind)).toBe(1);
    expect(maxAutoRetries('queue_full')).toBe(3);
    expect(error(400, 'capacity_exhausted', 'does not fit (requests past 10 tokens keep their KV on the SSD cache, which takes no writes)').kind).toBe('ssd_closed');
    expect(error(400, 'capacity_exhausted', 'the request does not fit in the memory').kind).toBe('capacity');
    expect(error(503, 'resource_timeout', "the SSD cache's room for active KV did not become available").kind).toBe('ssd_wait');
    expect(error(503, 'resource_timeout', 'memory did not become available').kind).toBe('resource_timeout');
  });
});

describe('a refused store size resets where it was set', () => {
  it('writes the model override, so the retry no longer passes the refused size', () => {
    const model = 'ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q2_0';
    const doc = { version: 4, global: { serve: {} }, models: { [model]: { serve: { expert_memory: '256M' } } } } as unknown as SettingsDoc;
    const next = applyPatch(doc, { 'serve.expert_memory': 'auto', 'serve.verify_rows': 'all' }, model) as unknown as {
      models: Record<string, { serve: Record<string, unknown> }>;
    };
    expect(next.models[model]!.serve.expert_memory).toBe('auto');
    expect(next.models[model]!.serve.verify_rows).toBe('all');
  });
});
