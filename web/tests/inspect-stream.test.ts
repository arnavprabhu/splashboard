/** The streamed compatibility check — the likely pick's verdict first, the rest after. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { InspectResult, VariantOut } from '../src/api/models';
import { ApiError } from '../src/api/client';
import { streamInspect } from '../src/routes/models/inspect';
import { inspectProgress, pickVariant } from '../src/routes/models/logic';

const REPO = 'unsloth/Qwen3.8-27B-GGUF';

function variant(name: string, loadable: boolean | null, recommended = false): VariantOut {
  return { name, size_bytes: 1, loadable, recommended, files: [] } as unknown as VariantOut;
}

function partial(variants: VariantOut[], extra: Partial<InspectResult> = {}): InspectResult {
  return {
    id: REPO,
    repo_id: REPO,
    compatible: false,
    badge: 'checking',
    variants,
    vision: { available: false },
    cached: false,
    checked_at: '',
    pending: variants.filter((v) => v.loadable === null).map((v) => v.name),
    ...extra,
  } as InspectResult;
}

function sse(events: [string, unknown][]): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const [name, data] of events) controller.enqueue(enc.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

afterEach(() => vi.restoreAllMocks());

describe('streamInspect', () => {
  it('reports each partial result in order and resolves with the complete one', async () => {
    const snapshot = partial([variant('Q8_0', null), variant('UD-Q4_K_M', null)]);
    const first = partial([variant('Q8_0', null), variant('UD-Q4_K_M', true, true)], { badge: 'compatible', compatible: true, recommended_variant: 'UD-Q4_K_M' });
    const done = { ...first, variants: [variant('Q8_0', true), variant('UD-Q4_K_M', true, true)], pending: [] };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sse([
        ['inspect.progress', snapshot],
        ['inspect.progress', first],
        ['inspect.result', done],
      ]),
    );
    const seen: InspectResult[] = [];
    const result = await streamInspect(REPO, { onProgress: (r) => seen.push(r) });
    expect(String(fetchMock.mock.calls[0]![0])).toBe(`/api/admin/inspect/stream?id=${encodeURIComponent(REPO)}`);
    // It runs Splash's helper, so it is a read-only POST like /inspect, never a GET.
    expect(fetchMock.mock.calls[0]![1]!.method).toBe('POST');
    expect(seen.map((r) => r.badge)).toEqual(['checking', 'compatible']);
    expect(seen[1]!.variants!.filter((v) => v.loadable !== null).map((v) => v.name)).toEqual(['UD-Q4_K_M']);
    expect(result.pending).toEqual([]);
    expect(result.variants!.every((v) => v.loadable !== null)).toBe(true);
  });

  it('turns inspect.error into an ApiError with the manager’s message', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sse([['inspect.error', { error: { message: 'Compatibility check timed out', type: 'overloaded_error', code: 'inspect_timeout' } }]]),
    );
    const error = await streamInspect(REPO).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe('Compatibility check timed out');
  });

  it('fails a stream that ends without a result', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(sse([['inspect.progress', partial([variant('Q8_0', null)])]]));
    await expect(streamInspect(REPO)).rejects.toBeInstanceOf(ApiError);
  });
});

describe('a partial result in the pages', () => {
  it('pickVariant waits for a verdict instead of defaulting to an unchecked row', () => {
    const snapshot = partial([variant('Q8_0', null), variant('UD-Q4_K_M', null)]);
    expect(pickVariant(snapshot, null)).toBeNull();
    const first = partial([variant('Q8_0', null), variant('UD-Q4_K_M', true, true)], { recommended_variant: 'UD-Q4_K_M' });
    expect(pickVariant(first, null)).toBe('UD-Q4_K_M');
    // A requested variant is shown even before its verdict; the button waits for it.
    expect(pickVariant(snapshot, 'q8_0')).toBe('Q8_0');
    // A catalog row (no `pending`): unchecked rows are still downloadable defaults.
    expect(pickVariant({ variants: [variant('Q8_0', null)], recommended_variant: null }, null)).toBe('Q8_0');
  });

  it('inspectProgress counts checked variants until nothing is pending', () => {
    const first = partial([variant('Q8_0', null), variant('UD-Q4_K_M', true), variant('mmproj-BF16', null)], { pending: ['Q8_0'] });
    expect(inspectProgress(first)).toEqual({ checked: 1, total: 2 });
    expect(inspectProgress({ ...first, pending: [] })).toBeNull();
    expect(inspectProgress(null)).toBeNull();
  });
});
