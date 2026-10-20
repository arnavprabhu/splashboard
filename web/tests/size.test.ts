import { describe, expect, it } from 'vitest';
import { parseExpertMemory, parseMaxCacheDisk, parseMaxCacheDiskWrites, parseMaxContext, parseMaxMemory, parsePleMemory, parseRequestSize, parseSize, parseSparseKvMemory } from '../src/lib/size';

const G = 1024 ** 3;

describe('parseMaxMemory (serve_options.parse_max_memory)', () => {
  it.each([
    ['auto', null],
    [' AUTO ', null],
    ['32G', 32 * G],
    ['32g', 32 * G],
    ['32GB', 32 * G],
    ['32GiB', 32 * G],
    ['512M', 512 * 1024 ** 2],
    ['64K', 64 * 1024],
    ['1048576', 1048576],
    // A fraction before a unit (Splash 1.4.0), rounded down to whole bytes.
    ['1.5G', 1.5 * G],
    ['2.5g', 2.5 * G],
    ['0.5K', 512],
    ['1.0009765625K', 1025],
    ['1.0009K', 1024],
    // Python's decimal context: the product keeps 28 significant digits, then truncates.
    ['1.99999999999999999999999999999G', 2 * G],
  ])('accepts %s', (input, bytes) => {
    expect(parseMaxMemory(input)).toEqual({ ok: true, bytes });
  });
  // Signs, underscores and inner spaces are refused from 1.4.0, as is a fraction without a unit.
  it.each(['0', '-1G', '+5G', '1_000', '32 G', '2.5', '.5G', '5.G', 'G', '', '32T', 'abc', '1__0'])('rejects %s', (input) => {
    const r = parseMaxMemory(input);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toBe("must be 'auto' or a positive byte count such as 32G or 2.5G");
  });
});

describe('parseMaxCacheDisk', () => {
  it('accepts 0 to disable and sizes', () => {
    expect(parseMaxCacheDisk('0')).toEqual({ ok: true, bytes: 0 });
    expect(parseMaxCacheDisk('32G')).toEqual({ ok: true, bytes: 32 * G });
  });
  it('accepts auto and words its error like Splash', () => {
    expect(parseMaxCacheDisk('auto')).toEqual({ ok: true, bytes: null });
    expect(parseMaxCacheDisk(' AUTO ')).toEqual({ ok: true, bytes: null });
    expect(parseMaxCacheDisk('-1G')).toEqual({ ok: false, error: 'use auto, 0 to disable, or a size such as 16G' });
  });
});

describe('parseMaxCacheDiskWrites', () => {
  it('accepts 0 (no limit) and sizes, refuses auto', () => {
    expect(parseMaxCacheDiskWrites('0')).toEqual({ ok: true, bytes: 0 });
    expect(parseMaxCacheDiskWrites('200G')).toEqual({ ok: true, bytes: 200 * G });
    expect(parseMaxCacheDiskWrites('auto')).toEqual({ ok: false, error: 'use 0 for no limit, or a size such as 200G' });
  });
});

describe('parseRequestSize', () => {
  it('accepts sizes and rejects auto/zero', () => {
    expect(parseRequestSize('128M')).toEqual({ ok: true, bytes: 128 * 1024 ** 2 });
    expect(parseRequestSize('auto').ok).toBe(false);
    expect(parseRequestSize('0').ok).toBe(false);
  });
  it('dispatches by kind', () => {
    expect(parseSize('request-size', '1K')).toEqual({ ok: true, bytes: 1024 });
    expect(parseSize('max-memory', 'auto')).toEqual({ ok: true, bytes: null });
    expect(parseSize('max-cache-disk', '0')).toEqual({ ok: true, bytes: 0 });
    expect(parseSize('expert-memory', 'off')).toEqual({ ok: true, bytes: 0 });
    expect(parseSize('sparse-kv-memory', '1200M')).toEqual({ ok: true, bytes: 1200 * 1024 ** 2 });
  });
});

describe('the streamed stores (serve_options._memory_size)', () => {
  it('take auto, off where the store has it, or a size', () => {
    expect(parseExpertMemory(' AUTO ')).toEqual({ ok: true, bytes: null });
    expect(parseExpertMemory('OFF')).toEqual({ ok: true, bytes: 0 });
    expect(parseExpertMemory('16G')).toEqual({ ok: true, bytes: 16 * G });
    expect(parsePleMemory('2.5G')).toEqual({ ok: true, bytes: 2.5 * G });
    expect(parseSparseKvMemory('auto')).toEqual({ ok: true, bytes: null });
  });
  it('word their errors like Splash', () => {
    expect(parseExpertMemory('0')).toEqual({ ok: false, error: 'use auto, off, or a size such as 16G' });
    expect(parsePleMemory('lots')).toEqual({ ok: false, error: 'use auto, off, or a size such as 2G' });
    expect(parseSparseKvMemory('off')).toEqual({ ok: false, error: 'use auto, or a size such as 1200M' });
  });
});

describe('parseMaxContext (serve_options.parse_max_context)', () => {
  it.each([
    ['auto', null],
    ['100K', 102400],
    ['256k', 262144],
    ['262144', 262144],
    [' 64 K', 65536],
    // Past the native window with YaRN (Splash 1.4.0): M is a million tokens.
    ['257K', 263168],
    ['512K', 524288],
    ['1M', 1000000],
    ['1m', 1000000],
    ['986K', 1009664],
    ['1010000', 1010000],
  ])('accepts %s', (input, tokens) => {
    expect(parseMaxContext(input)).toEqual({ ok: true, tokens });
  });
  it.each(['0', '987K', '1010001', '2M', '1.5M', '1.5K', 'K', 'M', '', '-1'])('rejects %s', (input) => {
    expect(parseMaxContext(input)).toEqual({
      ok: false,
      error: "must be 'auto' or a token count up to 1010000, such as 100K or 1M (K = 1024 tokens, M = 1000000 tokens)",
    });
  });
});
