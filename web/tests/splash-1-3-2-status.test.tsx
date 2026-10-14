/**
 * What Splash 1.3.2's `/status` and engine log add on screen: the SSD cache drawn against what it
 * may hold now and its write gate, the Active requests table, the host's spare memory, a weights
 * restore under way, the state format in the header, deduplicated tokens, and the Logs page's
 * classes for the new Started and SSD cache lines.
 */
import { render, screen } from '@testing-library/preact';
import { beforeEach, describe, expect, it } from 'vitest';
import type { LiveMetrics } from '../src/api/models';
import { engineLevel, tagFor } from '../src/routes/logs/model';
import { ActiveRequestsBand, CacheBand, MemoryBand, weightsItem } from '../src/routes/status/Bands';
import { activeRequests, diskEnabled, diskWrites, metaKeys, promptProgress } from '../src/routes/status/logic';
import { sessionStats } from '../src/routes/status/Numbers';
import { settings } from '../src/store';
import { liveSample } from '../src/store/live';

const GiB = 1024 ** 3;

beforeEach(() => {
  settings.value = { settings: { version: 1, global: {}, models: {} } } as never;
  liveSample.value = null;
});

describe('SSD cache on or off', () => {
  it('follows the engine once it reports disk, the setting before', () => {
    expect(diskEnabled({ disk: { capacity_bytes: 32 * GiB, limit_bytes: 8 * GiB } }, '0')).toBe(true);
    expect(diskEnabled({ disk: { capacity_bytes: 0, limit_bytes: 0 } }, 'auto')).toBe(false);
    expect(diskEnabled({ disk: { capacity_bytes: 0, limit_bytes: GiB } }, 'auto')).toBe(true);
    expect(diskEnabled(null, 'auto')).toBe(true);
    expect(diskEnabled(null, '0')).toBe(false);
    expect(diskEnabled(null, undefined)).toBe(false);
  });
});

describe('SSD cache writes', () => {
  it('reads the gate and the budget left', () => {
    expect(diskWrites({ disk: { writes_gated: 'no', write_budget_bytes: 0 } })).toBe('open');
    expect(diskWrites({ disk: { writes_gated: 'no', write_budget_bytes: GiB } })).toBe('budget_left');
    expect(diskWrites({ disk: { writes_gated: 'budget', write_budget_bytes: 0 } })).toBe('budget_spent');
    expect(diskWrites({ disk: { writes_gated: 'closed' } })).toBe('closed');
    expect(diskWrites({ disk: {} })).toBeNull();
  });
});

describe('Cache band', () => {
  it('draws the meter against the live limit and names the quota when free space limits it', () => {
    const raw = {
      disk: {
        capacity_bytes: 32 * GiB,
        limit_bytes: 8 * GiB,
        used_bytes: 2 * GiB,
        volume_available_bytes: 43 * GiB,
        writes_gated: 'budget',
        write_budget_bytes: 0,
      },
    };
    render(<CacheBand raw={raw} stopped={false} />);
    expect(screen.getAllByText(/2\.0 GB of 8\.0 GB|2 GB of 8 GB/).length).toBeGreaterThan(0);
    expect(screen.getByTestId('ssd-limited').textContent).toContain('quota');
    expect(screen.getByTestId('ssd-writes').textContent).toBe('write budget spent · reusable states wait for it');
  });

  it('shows a closed gate in the accent and no quota line at the full quota', () => {
    render(<CacheBand raw={{ disk: { capacity_bytes: 8 * GiB, limit_bytes: 8 * GiB, used_bytes: 0, writes_gated: 'closed' } }} stopped={false} />);
    expect(screen.queryByTestId('ssd-limited')).toBeNull();
    const closed = screen.getByText('stopped taking writes this session');
    expect(closed.getAttribute('data-accent')).toBe('true');
  });

  it('is off when the engine reports a cache of 0, whatever the setting', () => {
    settings.value = { settings: { version: 1, global: { serve: { max_cache_disk: 'auto' } }, models: {} } } as never;
    render(<CacheBand raw={{ disk: { capacity_bytes: 0, limit_bytes: 0 } }} stopped={false} />);
    expect(screen.queryByTestId('ssd-writes')).toBeNull();
    expect(screen.getAllByText(/^Off$|Off ·/).length).toBeGreaterThan(0);
  });
});

describe('Active requests', () => {
  const requests = [
    { id: 7, phase: 'prefill', priority: 'foreground', prompt_tokens: 4096, prompt_processed: 2048, generated_tokens: 0, max_new_tokens: 256, age_ms: 1500.5 },
    { id: 9, phase: 'decode', priority: 'normal', prompt_tokens: 512, prompt_processed: 512, generated_tokens: 40, max_new_tokens: 128, age_ms: 2250.25 },
  ];

  it('prefers the live sample and falls back to the raw document', () => {
    expect(activeRequests({ active_requests: [requests[0]!] }, { active_requests: requests })).toHaveLength(1);
    expect(activeRequests(null, { active_requests: [...requests, { phase: 'decode' }, 'junk'] })).toHaveLength(2);
    expect(activeRequests(null, {})).toEqual([]);
    expect(promptProgress({ prompt_tokens: 4096, prompt_processed: 2048 })).toBe(0.5);
    expect(promptProgress({ prompt_tokens: 0, prompt_processed: 0 })).toBeNull();
  });

  it('lists each request and hides when there are none', () => {
    liveSample.value = { active_requests: requests } as unknown as LiveMetrics;
    render(<ActiveRequestsBand raw={null} stopped={false} />);
    expect(screen.getAllByText('Active requests').length).toBeGreaterThan(0);
    expect(screen.getByText('2 requests the engine holds now')).toBeTruthy();
    expect(screen.getByText('Prefill')).toBeTruthy();
    expect(screen.getByText('Decode')).toBeTruthy();
    expect(screen.getByText('2,048 / 4,096 · 50%')).toBeTruthy();
    expect(screen.getByText('40 / 128')).toBeTruthy();
  });

  it('renders nothing without requests or while stopped', () => {
    liveSample.value = { active_requests: [] } as unknown as LiveMetrics;
    const empty = render(<ActiveRequestsBand raw={null} stopped={false} />);
    expect(empty.container.textContent).toBe('');
    liveSample.value = { active_requests: requests } as unknown as LiveMetrics;
    const stopped = render(<ActiveRequestsBand raw={null} stopped />);
    expect(stopped.container.textContent).toBe('');
  });
});

describe('Memory band', () => {
  it('shows the host spare memory and what growth has room for', () => {
    const raw = {
      memory_governor: {
        limit_bytes: 24 * GiB,
        charged_bytes: 4 * GiB,
        host_available_bytes: 8 * GiB,
        host_reserve_bytes: 2 * GiB,
        host_spare_bytes: 8 * GiB,
        host_spare_reserve_bytes: 2 * GiB,
        host_headroom_bytes: 6 * GiB,
      },
    };
    render(<MemoryBand raw={raw} stopped={false} />);
    expect(screen.getByText('Host spare')).toBeTruthy();
    expect(screen.getByText(/keeps .* for other apps/)).toBeTruthy();
    expect(screen.getByText(/room for growth/)).toBeTruthy();
  });

  it('names a restore under way, in the accent, though the weights still read released', () => {
    const item = weightsItem({ weights: { released: true, restoring: true, idle_release_seconds: 600, restores: 1 } });
    expect(item.value).toBe('restoring for a request…');
    expect(item.accent).toBe(true);
    const released = weightsItem({ weights: { released: true, restoring: false, idle_release_seconds: null } });
    expect(released.value).toBe('released · reload on the next request');
    expect(released.accent).toBe(false);
  });
});

describe('header and numbers', () => {
  it('puts the state format right after the KV format', () => {
    expect(metaKeys({ format: 'MLX', context: 1000, kv: 'int8', states: 'bf16', vision: true })).toEqual(['format', 'context', 'kv', 'states', 'vision']);
    expect(metaKeys({ kv: 'int8', states: null })).toEqual(['kv']);
  });

  it('adds the deduplicated tokens to the Cached sub-line', () => {
    const sample = { totals: { reused_tokens: 3000, deduplicated_kv_tokens: 640 }, cache: {}, throughput: {}, draft: {}, latency: {} } as unknown as LiveMetrics;
    const cached = sessionStats(sample, { cache: { kv_hit_tokens: 3000, kv_disk_hit_tokens: 0 } }).find((s) => s.key === 'cached');
    expect(String(cached?.sub)).toContain('shared 640');
  });
});

describe('Logs classifier, Splash 1.3.2 lines', () => {
  it('counts Started lines as requests', () => {
    expect(engineLevel('info', 'stdout', 'Started · input 1,024 · cached 512 · effort absent → high · thinking on')).toBe('req');
    expect(engineLevel('info', 'stdout', 'Done · input 1,024 · cached 512 · deduplicated 64 · output 20 · 2.4 tok/cycle')).toBe('req');
  });

  it('tags the SSD cache startup lines and warns when the cache is off', () => {
    const on = 'SSD cache: up to 8.0 GiB of temporary files in /tmp (its automatic quota, while its volume has room beyond the 20.0 GiB it keeps free); states of 300 MiB; --max-cache-disk 0 turns it off.';
    expect(tagFor(on)).toBe('ssd');
    expect(engineLevel('info', 'stdout', on)).toBe('info');
    const off = 'SSD cache off: its quota holds no state of 300 MiB; no state staging is set aside.';
    expect(tagFor(off)).toBe('ssd');
    expect(engineLevel('info', 'stdout', off)).toBe('warn');
    const persistent = 'Persistent cache off: the folder is not writable; the SSD cache keeps temporary files.';
    expect(tagFor(persistent)).toBe('ssd');
    expect(engineLevel('info', 'stdout', persistent)).toBe('warn');
  });

  it('tags and warns on the SSD cache write failures', () => {
    const closed = 'An SSD cache file takes no more writes after one failed (No space left on device); what it holds stays readable.';
    expect(tagFor(closed)).toBe('ssd');
    expect(engineLevel('info', 'stdout', closed)).toBe('warn');
    const punch = "Freed cache slots cannot return their blocks on this volume (Operation not supported); disk use may exceed the SSD cache's limit.";
    expect(tagFor(punch)).toBe('ssd');
    expect(engineLevel('info', 'stdout', punch)).toBe('warn');
  });

  it('tags a release on request as a weights event', () => {
    expect(tagFor('Weights released on request; the next request restores them')).toBe('load');
    expect(tagFor('Weights released after 600 s without a request; the next request restores them')).toBe('load');
  });
});
