import { describe, expect, it } from 'vitest';
import type { UsagePoint, UsageSummary } from '../src/api/models';
import {
  apiFilters,
  clientRows,
  dayEndIso,
  dayStartIso,
  defaultRange,
  exportHref,
  heatMax,
  heatOpacity,
  injectedCount,
  isFiltered,
  pageRange,
  readFilters,
  requestsOverTime,
  stackTokensPerDay,
} from '../src/routes/status/history';

const NOW = new Date(2026, 9, 3, 12, 0, 0);

const point = (t: string, group: string | null, prompt: number, completion: number, requests = 1, errors = 0): UsagePoint => ({
  t,
  group,
  requests,
  errors,
  cancelled: 0,
  prompt_tokens: prompt,
  cached_tokens: 0,
  completion_tokens: completion,
});

describe('history filters', () => {
  it('defaults to the last 30 days, inclusive of today', () => {
    expect(defaultRange(NOW)).toEqual({ from: '2026-09-04', to: '2026-10-03' });
    const f = readFilters(new URLSearchParams(), NOW);
    expect(f).toMatchObject({ from: '2026-09-04', to: '2026-10-03', model: '', endpoint: '', status: '', client: '' });
    expect(isFiltered(f, NOW)).toBe(false);
  });

  it('reads the query string, ignores malformed dates and orders the range', () => {
    const f = readFilters(new URLSearchParams('from=2026-10-02&to=2026-09-26&model=a/b&status=5xx&from_x=1'), NOW);
    expect(f.from).toBe('2026-09-26');
    expect(f.to).toBe('2026-10-02');
    expect(f.model).toBe('a/b');
    expect(isFiltered(f, NOW)).toBe(true);
    expect(readFilters(new URLSearchParams('from=yesterday'), NOW).from).toBe('2026-09-04');
  });

  it('maps a day range onto local midnight-to-midnight timestamps', () => {
    const start = new Date(dayStartIso('2026-09-26'));
    const end = new Date(dayEndIso('2026-10-03'));
    expect([start.getFullYear(), start.getMonth(), start.getDate(), start.getHours()]).toEqual([2026, 8, 26, 0]);
    expect([end.getDate(), end.getHours(), end.getMinutes(), end.getSeconds()]).toEqual([3, 23, 59, 59]);
  });

  it('sends only the active filters, under the manager names start/end', () => {
    const q = apiFilters({ from: '2026-09-26', to: '2026-10-03', model: '', endpoint: '/v1/messages', status: '', client: '' });
    expect(Object.keys(q).filter((k) => q[k] !== undefined)).toEqual(['start', 'end', 'endpoint']);
  });

  it('builds the export link with the same filters', () => {
    const href = exportHref({ from: '2026-09-26', to: '2026-10-03', model: 'm/x', endpoint: '', status: '4xx', client: '' });
    const url = new URL(href, 'http://h');
    expect(url.pathname).toBe('/api/admin/usage/export.csv');
    expect(url.searchParams.get('model')).toBe('m/x');
    expect(url.searchParams.get('status')).toBe('4xx');
    expect(url.searchParams.get('start')).toBe(dayStartIso('2026-09-26'));
    expect(url.searchParams.get('end')).toBe(dayEndIso('2026-10-03'));
    expect(url.searchParams.has('client')).toBe(false);
  });
});

describe('tokens per day', () => {
  const pts = [
    point('2026-10-01T00:00:00+00:00', 'a/active', 100, 10),
    point('2026-10-01T00:00:00+00:00', 'b/big', 500, 50),
    point('2026-10-01T00:00:00+00:00', 'c/small', 5, 0),
    point('2026-10-01T00:00:00+00:00', 'd/tiny', 1, 0),
    point('2026-10-02T00:00:00+00:00', 'a/active', 200, 20),
  ];

  it('puts the active model at the bottom in the accent, one more model, then "other" in mute', () => {
    const s = stackTokensPerDay(pts, 'a/active', 'other');
    // largest (top of the stack) first, so it is drawn first and shorter bars sit on top
    expect(s.series.map((x) => [x.label, x.tone])).toEqual([
      ['other', 'mute'],
      ['b/big', 'ink'],
      ['a/active', 'acc'],
    ]);
    expect(s.data[0]).toEqual([Date.parse('2026-10-01T00:00:00Z') / 1000, Date.parse('2026-10-02T00:00:00Z') / 1000]);
    expect(s.data[3]).toEqual([110, 220]); // active
    expect(s.data[2]).toEqual([660, 220]); // + big
    expect(s.data[1]).toEqual([666, 220]); // + other
  });

  it('has no accent when the active model is not in range', () => {
    const s = stackTokensPerDay(pts, 'z/none', 'other');
    expect(s.series.some((x) => x.tone === 'acc')).toBe(false);
    expect(s.series.map((x) => x.label)).toEqual(['other', 'a/active', 'b/big']);
  });

  it('is empty for no points', () => {
    expect(stackTokensPerDay([], null, 'other')).toEqual({ data: [[]], series: [] });
  });
});

describe('requests over time', () => {
  it('sums requests and failures across groups per bucket', () => {
    const cols = requestsOverTime([
      point('2026-10-02T00:00:00+00:00', 'a', 0, 0, 3, 1),
      point('2026-10-01T00:00:00+00:00', 'a', 0, 0, 2, 0),
      point('2026-10-02T00:00:00+00:00', 'b', 0, 0, 4, 2),
    ]);
    expect(cols[1]).toEqual([2, 7]);
    expect(cols[2]).toEqual([0, 3]);
  });
});

describe('heatmap scale', () => {
  it('uses ink opacity 0.08–1.0, and none for an empty hour', () => {
    expect(heatOpacity(0, 10)).toBe(0);
    expect(heatOpacity(10, 10)).toBe(1);
    expect(heatOpacity(1, 1000)).toBeCloseTo(0.081, 3);
    expect(heatOpacity(5, 10)).toBeCloseTo(0.54, 3);
    expect(heatOpacity(3, 0)).toBe(0);
    expect(heatMax([[1, 9], [4, 0]])).toBe(9);
  });
});

describe('top clients and injected fields', () => {
  it('reads tokens and last seen from the summary', () => {
    const summary = {
      top_clients: [
        { client: 'a', requests: 2, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
        { client: 'b', requests: 1, prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, last_seen_at: '2026-10-03T00:00:00Z' },
      ],
    } as unknown as UsageSummary;
    expect(clientRows(summary)).toEqual([
      { client: 'a', requests: 2, tokens: 0, last_seen: null },
      { client: 'b', requests: 1, tokens: 15, last_seen: '2026-10-03T00:00:00Z' },
    ]);
    expect(clientRows(null)).toEqual([]);
  });

  it('describes the visible row range', () => {
    expect(pageRange(0, 50)).toEqual({ a: 1, b: 50 });
    expect(pageRange(1, 12)).toEqual({ a: 51, b: 62 });
    expect(pageRange(0, 0)).toEqual({ a: 0, b: 0 });
  });

  it('counts injected fields', () => {
    expect(injectedCount(null)).toBe(0);
    expect(injectedCount({ temperature: 0.7, top_p: 0.8 })).toBe(2);
  });
});
