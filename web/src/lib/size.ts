/**
 * Client-side mirror of Splash's size parsers (splash/server/serve_options.py:
 * parse_max_context, parse_max_memory, parse_max_cache_disk, parse_request_size). The manager validates
 * again on save; this is for inline feedback only.
 */

const UNITS: Record<string, number> = {};
(['K', 'M', 'G'] as const).forEach((unit, i) => {
  for (const suffix of ['', 'B', 'IB']) UNITS[unit + suffix] = 1024 ** (i + 1);
});
const SUFFIXES = Object.keys(UNITS).sort((a, b) => b.length - a.length);

/** Python int() accepts surrounding whitespace, a sign and underscores between digits. */
const PY_INT = /^\s*[+-]?\d+(?:_\d+)*\s*$/;
const MAX_I64 = 2n ** 63n - 1n;

export type SizeResult = { ok: true; bytes: number | null } | { ok: false; error: string };

function parseBytes(value: string): bigint | null {
  let normalized = value.trim().toUpperCase();
  let multiplier = 1n;
  for (const suffix of SUFFIXES) {
    if (normalized.endsWith(suffix)) {
      normalized = normalized.slice(0, -suffix.length);
      multiplier = BigInt(UNITS[suffix] ?? 1);
      break;
    }
  }
  if (!PY_INT.test(normalized)) return null;
  const size = BigInt(normalized.trim().replace(/_/g, '')) * multiplier;
  return size >= 1n && size <= MAX_I64 ? size : null;
}

/** `--max-memory`: 'auto' (null) or a positive byte count such as 32G. */
export function parseMaxMemory(value: string): SizeResult {
  if (value.trim().toUpperCase() === 'AUTO') return { ok: true, bytes: null };
  const size = parseBytes(value);
  return size === null
    ? { ok: false, error: "must be 'auto' or a positive byte count such as 32G" }
    : { ok: true, bytes: Number(size) };
}

/** `--max-cache-disk`: 0 disables, otherwise a size such as 5G. */
export function parseMaxCacheDisk(value: string): SizeResult {
  if (value.trim() === '0') return { ok: true, bytes: 0 };
  const size = value.trim().toUpperCase() === 'AUTO' ? null : parseBytes(value);
  return size === null
    ? { ok: false, error: 'use 0 to disable, or a size such as 5G' }
    : { ok: true, bytes: Number(size) };
}

/** `--max-request-size`: a positive byte count such as 128M. */
export function parseRequestSize(value: string): SizeResult {
  const size = value.trim().toUpperCase() === 'AUTO' ? null : parseBytes(value);
  return size === null
    ? { ok: false, error: 'must be a positive byte count such as 128M' }
    : { ok: true, bytes: Number(size) };
}

/** splash/server/serve_options.py MAX_CONTEXT_TOKENS (256K). */
export const MAX_CONTEXT_TOKENS = 262144;

export type TokensResult = { ok: true; tokens: number | null } | { ok: false; error: string };

/** `--max-context`: 'auto' (null) or a token count up to 256K; K is 1024 tokens. */
export function parseMaxContext(value: string): TokensResult {
  const normalized = value.trim().toUpperCase();
  if (normalized === 'AUTO') return { ok: true, tokens: null };
  const kilo = normalized.endsWith('K');
  const digits = kilo ? normalized.slice(0, -1) : normalized;
  const tokens = PY_INT.test(digits) ? Number(digits.trim().replace(/_/g, '')) * (kilo ? 1024 : 1) : 0;
  return tokens >= 1 && tokens <= MAX_CONTEXT_TOKENS
    ? { ok: true, tokens }
    : { ok: false, error: "must be 'auto' or a token count up to 256K, such as 100K" };
}

export type SizeKind = 'max-memory' | 'max-cache-disk' | 'request-size';

export function parseSize(kind: SizeKind, value: string): SizeResult {
  switch (kind) {
    case 'max-memory':
      return parseMaxMemory(value);
    case 'max-cache-disk':
      return parseMaxCacheDisk(value);
    case 'request-size':
      return parseRequestSize(value);
  }
}
