/**
 * What Splash 1.6.0 adds on screen: the expert store that grows and gives memory back, the
 * requests waiting for memory on Splash's ledger, where a first token's time went, a request
 * Splash refuses after it crashed the engine twice, PDFs whose sandbox could not start, and a
 * download of a repository Hugging Face does not have.
 */
import { render, screen } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import { ApiError } from '../src/api/client';
import { autoRetry, classifyError } from '../src/routes/chat/logic';
import { downloadErrorView } from '../src/routes/models/logic';
import { LatencyBand, StreamingBand, claimsItem, expertStore, ttftParts } from '../src/routes/status/Bands';

const GiB = 1024 ** 3;
const use = { loads: 4, routed: 100, read: 10, prefetched: 5, read_ahead: 0, hit_rate: 0.9, read_bytes: GiB, host_steps: {} };
const experts = (extra: Record<string, unknown> = {}) => ({
  arena_bytes: 3 * GiB,
  staging_bytes: 400 * 1024 ** 2,
  slots: 340,
  experts: 10240,
  resident: 340,
  prefill: use,
  decode: use,
  failure: '',
  ...extra,
});
const samples = (p50: number, n: number) => ({ p50, p95: p50 * 2, p99: p50 * 2, max: p50 * 3, total: p50 * n, samples: n });
const api = (status: number, code: string, message = 'x') => new ApiError(status, { message, type: 't', code }, null, null);

describe('the elastic expert store', () => {
  it('says how much of its cap the store holds, and its floor', () => {
    const store = experts({ cap_bytes: 8 * GiB, floor_bytes: GiB, chunks: 3, cap_chunks: 8, shrinks: 0, regrows: 0 });
    expect(expertStore(store)).toBe('store 3 GB of 8 GB, floor 1 GB');
    const { container } = render(<StreamingBand raw={{ streamed_experts: store }} stopped={false} />);
    expect(container.textContent).toContain('arena 3 GB · store 3 GB of 8 GB, floor 1 GB');
    expect(container.querySelector('[data-accent="true"]')).toBeNull();
  });

  it('counts the memory it gave back and took again', () => {
    const store = experts({ cap_bytes: 8 * GiB, floor_bytes: GiB, chunks: 4, cap_chunks: 8, shrinks: 2, regrows: 1 });
    expect(expertStore(store)).toBe('store 4 GB of 8 GB, floor 1 GB · gave back 2×, regrew 1×');
  });

  it('is fixed where a size sets it, and says nothing before 1.6.0', () => {
    expect(expertStore(experts({ cap_bytes: 4 * GiB, floor_bytes: 4 * GiB, chunks: 4, cap_chunks: 4 }))).toBe('store fixed at 4 GB');
    expect(expertStore(experts())).toBeNull();
    const { container } = render(<StreamingBand raw={{ streamed_experts: experts() }} stopped={false} />);
    expect(container.textContent).not.toContain('store');
  });
});

describe('requests waiting for memory', () => {
  const raw = (waiting: number) => ({ memory_governor: { claims: { waiting, bytes: waiting * GiB, oldest_ms: waiting ? 1250 : 0 } } });

  it('has no row before 1.6.0', () => {
    expect(claimsItem({ memory_governor: { limit_bytes: 1 } })).toEqual([]);
    expect(claimsItem(null)).toEqual([]);
  });

  it('says none, or how many wait, for how much and how long', () => {
    expect(claimsItem(raw(0))[0]).toMatchObject({ key: 'claims', label: 'Waiting for memory', value: 'no request', meta: undefined });
    expect(claimsItem(raw(1))[0]).toMatchObject({ value: '1 request, for 1 GB', meta: 'longest wait 1.25 s' });
    expect(claimsItem(raw(2))[0]).toMatchObject({ value: '2 requests, for 2 GB' });
  });
});

describe('where a first token’s time went', () => {
  const parts = { own_prefill: samples(520, 4), claim_wait: samples(0, 0), other_prefill: samples(90, 1), decode: samples(30, 4), rest: samples(16, 4) };

  it('is null before 1.6.0 and before any request reached a first token', () => {
    expect(ttftParts({ metrics: {} })).toBeNull();
    const idle = Object.fromEntries(Object.keys(parts).map((k) => [k, samples(0, 0)]));
    expect(ttftParts({ metrics: { ttft_parts_ms: idle } })).toBeNull();
  });

  it('gives each part’s median, a dash for a part no request had', () => {
    expect(ttftParts({ metrics: { ttft_parts_ms: parts } })).toEqual({ own_prefill: '520 ms', claim_wait: '—', other_prefill: '90 ms', decode: '30 ms', rest: '16 ms' });
    render(<LatencyBand raw={{ latency: {}, metrics: { ttft_parts_ms: parts } }} stopped={false} />);
    expect(screen.getByTestId('ttft-parts').textContent).toBe(
      'Engine time to first token (p50): own prefill 520 ms · waiting for memory — · other requests’ prefill 90 ms · decode 30 ms · rest 16 ms',
    );
  });

  it('shows no line without the breakdown', () => {
    render(<LatencyBand raw={{ latency: {} }} stopped={false} />);
    expect(screen.queryByTestId('ttft-parts')).toBeNull();
  });
});

describe('Chat errors from Splash 1.6.0', () => {
  it('explains a quarantined request and does not retry it', () => {
    const error = classifyError(api(400, 'request_quarantined', 'this request was in flight at two engine failures in a row'));
    expect(error).toMatchObject({ kind: 'quarantined', title: 'Splash refuses this request: it was running both times the engine just failed.' });
    expect(error.body).toContain('Restarting the engine');
    expect(autoRetry(error.kind)).toBe(false);
  });

  it('says PDFs can’t be read when the sandbox could not start', () => {
    const error = classifyError(api(503, 'document_unavailable', 'document worker could not start'));
    expect(error).toMatchObject({ kind: 'document_unavailable', title: 'PDFs can’t be read right now.' });
    expect(autoRetry(error.kind)).toBe(false);
  });
});

describe('a download of a repository Hugging Face does not have', () => {
  it('says so and offers no retry', () => {
    const view = downloadErrorView({ error: { code: 'not_found', message: 'cannot resolve o/r: Repository not found' } } as never, false);
    expect(view).toEqual({ text: 'Hugging Face has no repository with this ID. Check the model ID and download it again.', actions: ['details', 'remove'] });
  });
});
