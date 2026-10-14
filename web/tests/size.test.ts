import { describe, expect, it } from 'vitest';
import { parseMaxCacheDisk, parseMaxCacheDiskWrites, parseMaxContext, parseMaxMemory, parseRequestSize, parseSize } from '../src/lib/size';

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
    ['32 G', 32 * G],
    ['1_000', 1000],
    ['+5G', 5 * G],
  ])('accepts %s', (input, bytes) => {
    expect(parseMaxMemory(input)).toEqual({ ok: true, bytes });
  });
  it.each(['0', '-1G', '1.5G', 'G', '', '32T', 'abc', '1__0'])('rejects %s', (input) => {
    const r = parseMaxMemory(input);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toBe("must be 'auto' or a positive byte count such as 32G");
  });
});

describe('parseMaxCacheDisk', () => {
  it('accepts 0 to disable and sizes', () => {
    expect(parseMaxCacheDisk('0')).toEqual({ ok: true, bytes: 0 });
    expect(parseMaxCacheDisk('32G')).toEqual({ ok: true, bytes: 32 * G });
  });
  it('accepts auto (Splash 1.3.2) and words its error like Splash', () => {
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
  });
});

describe('parseMaxContext (serve_options.parse_max_context)', () => {
  it.each([
    ['auto', null],
    ['100K', 102400],
    ['256k', 262144],
    ['262144', 262144],
    [' 64 K', 65536],
  ])('accepts %s', (input, tokens) => {
    expect(parseMaxContext(input)).toEqual({ ok: true, tokens });
  });
  it.each(['0', '257K', '262145', '1.5K', 'K', '', '-1'])('rejects %s', (input) => {
    expect(parseMaxContext(input)).toEqual({ ok: false, error: "must be 'auto' or a token count up to 256K, such as 100K" });
  });
});
