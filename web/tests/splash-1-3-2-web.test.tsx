/**
 * Splash 1.3.2 on screen: why the SSD cache or its persistence is off this start, a Model
 * Optimizer checkpoint labelled by its quantization in the Status header, the thinking end text's
 * checks, and the benchmark's count of requests the context cut short.
 */
import { render, screen } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { BenchmarkRun, EngineNotice } from '../src/api/models';
import { CacheBand } from '../src/routes/status/Bands';
import { cacheOffReason, formatLabel } from '../src/routes/status/logic';
import { SPLASH_MESSAGES, fieldError } from '../src/routes/settings/validate';
import { truncatedRequests } from '../src/routes/tools-benchmark';
import { engine, settings } from '../src/store';

const notice = (kind: EngineNotice['kind'], line: string): EngineNotice => ({ kind, message: line, raw: line, ts: '2026-10-14T10:00:00Z' });

beforeEach(() => {
  settings.value = { settings: { version: 3, global: {}, models: {} } } as never;
});

afterEach(() => {
  engine.value = null as never;
});

describe('SSD cache off this start', () => {
  it('takes the reason from the engine line, without what Splash adds after it', () => {
    const quota = notice('disk_cache_off', 'SSD cache off: its quota holds no state of 153 MiB; no state staging is set aside.');
    expect(cacheOffReason([quota], 'disk_cache_off')).toBe('its quota holds no state of 153 MiB');
    const both = notice(
      'disk_cache_off',
      'SSD cache off: for its root, the volume is full; for temporary files, it is read-only; the state staging the plan set aside stays unused.',
    );
    expect(cacheOffReason([quota, both], 'disk_cache_off')).toBe('for its root, the volume is full; for temporary files, it is read-only');
    const persistent = notice('persistent_cache_off', 'Persistent cache off: its root is on a network volume; the SSD cache keeps temporary files.');
    expect(cacheOffReason([persistent], 'persistent_cache_off')).toBe('its root is on a network volume');
    expect(cacheOffReason([persistent], 'disk_cache_off')).toBeNull();
    expect(cacheOffReason(null, 'disk_cache_off')).toBeNull();
  });

  it('shows the reasons in the Cache band', () => {
    engine.value = {
      state: 'ready',
      model: 'org/m',
      view: {
        notices: [
          notice('disk_cache_off', 'SSD cache off: its quota holds no state of 153 MiB; no state staging is set aside.'),
          notice('persistent_cache_off', 'Persistent cache off: its root is on a network volume; the SSD cache keeps temporary files.'),
        ],
      },
    } as never;
    render(<CacheBand raw={{ disk: { capacity_bytes: 0, limit_bytes: 0, persistent: false } }} stopped={false} />);
    expect(screen.getByTestId('ssd-off-reason').textContent).toBe('Splash turned it off this start: its quota holds no state of 153 MiB.');
    expect(screen.getByTestId('persistent-off-reason').textContent).toContain('its root is on a network volume');
  });
});

describe('Status header format', () => {
  it('names a Model Optimizer checkpoint by its quantization, other MLX models as MLX', () => {
    expect(formatLabel({ format: 'mlx', variant: null, checkpoint: 'NVFP4 · Model Optimizer' } as never)).toBe('NVFP4');
    expect(formatLabel({ format: 'mlx', variant: null, checkpoint: 'FP8 · compressed-tensors' } as never)).toBe('FP8');
    expect(formatLabel({ format: 'mlx', variant: null } as never)).toBe('MLX');
    expect(formatLabel({ format: 'gguf', variant: 'Q4_K_M' } as never)).toBe('GGUF Q4_K_M');
  });
});

describe('thinking end text', () => {
  const rule = { key: 'serve.thinking_end_text', control: 'text' };
  it('takes blank lines before </think>, and refuses a NUL or a missing </think>', () => {
    expect(fieldError(rule, null)).toBeNull();
    expect(fieldError(rule, '\n\nTime to answer.\n</think>')).toBeNull();
    expect(fieldError(rule, 'tab\there </think>')).toBeNull();
    expect(fieldError(rule, 'a\u0000b</think>')).toBe(SPLASH_MESSAGES.thinkingEndTextNul);
    expect(fieldError(rule, 'no ending')).toBe(SPLASH_MESSAGES.thinkingEndText);
  });
});

describe('benchmark truncated requests', () => {
  it('adds up the requests Splash capped at the context', () => {
    const run = {
      results: [
        { scenario: 'decode_short', params: {}, samples: [], summary: { truncated_requests: 2 } },
        { scenario: 'concurrency', params: {}, samples: [], summary: { truncated_requests: 1 } },
        { scenario: 'cold_prefill', params: {}, samples: [], summary: {} },
      ],
    } as unknown as BenchmarkRun;
    expect(truncatedRequests(run)).toBe(3);
    expect(truncatedRequests({ results: [] } as unknown as BenchmarkRun)).toBe(0);
  });
});
