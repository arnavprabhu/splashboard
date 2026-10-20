/**
 * Client-side mirror of Splash's size parsers (splash/server/serve_options.py:
 * parse_max_context, parse_max_memory, parse_max_cache_disk, parse_max_cache_disk_writes,
 * parse_request_size and the `_memory_size` of --expert-memory, --ple-memory and
 * --sparse-kv-memory). The manager validates again on save; this is for inline feedback only.
 */

const UNITS: Record<string, number> = {};
(['K', 'M', 'G'] as const).forEach((unit, i) => {
  for (const suffix of ['', 'B', 'IB']) UNITS[unit + suffix] = 1024 ** (i + 1);
});
const SUFFIXES = Object.keys(UNITS).sort((a, b) => b.length - a.length);

/** Python int() accepts surrounding whitespace, a sign and underscores between digits. */
const PY_INT = /^\s*[+-]?\d+(?:_\d+)*\s*$/;
/** A size's number: whole, or with a fraction before a unit (2.5G). */
const SIZE_NUMBER = /^(\d+)(?:\.(\d+))?$/;
const MAX_I64 = 2n ** 63n - 1n;
/** Python's default decimal context keeps 28 significant digits of a product. */
const DECIMAL_DIGITS = 28;

export type SizeResult = { ok: true; bytes: number | null } | { ok: false; error: string };

/** `int(Decimal(digits) / 10**fraction * multiplier)` as Python computes it: the product rounded
 * half-even to 28 significant digits, then truncated to whole bytes. */
function decimalBytes(whole: string, fraction: string, multiplier: bigint): bigint {
  let product = BigInt(whole + fraction) * multiplier;
  const digits = product.toString().length;
  if (product > 0n && digits > DECIMAL_DIGITS) {
    const drop = 10n ** BigInt(digits - DECIMAL_DIGITS);
    let kept = product / drop;
    const rest = product % drop;
    const half = drop / 2n;
    if (rest > half || (rest === half && kept % 2n === 1n)) kept += 1n;
    product = kept * drop;
  }
  return product / 10n ** BigInt(fraction.length);
}

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
  const number = SIZE_NUMBER.exec(normalized);
  // A fraction needs a unit: 2.5G is a size, 2.5 is not.
  if (!number || (multiplier === 1n && number[2] !== undefined)) return null;
  const size = decimalBytes(number[1] ?? '0', number[2] ?? '', multiplier);
  return size >= 1n && size <= MAX_I64 ? size : null;
}

/** `--max-memory`: 'auto' (null) or a positive byte count such as 32G or 2.5G. */
export function parseMaxMemory(value: string): SizeResult {
  if (value.trim().toUpperCase() === 'AUTO') return { ok: true, bytes: null };
  const size = parseBytes(value);
  return size === null
    ? { ok: false, error: "must be 'auto' or a positive byte count such as 32G or 2.5G" }
    : { ok: true, bytes: Number(size) };
}

/** `--max-cache-disk`: 'auto' (null), 0 disables, otherwise a size such as 16G. */
export function parseMaxCacheDisk(value: string): SizeResult {
  if (value.trim() === '0') return { ok: true, bytes: 0 };
  const r = parseMaxMemory(value);
  return r.ok ? r : { ok: false, error: 'use auto, 0 to disable, or a size such as 16G' };
}

/** `--max-cache-disk-writes`: 0 for no daily limit, otherwise a size such as 200G (never 'auto'). */
export function parseMaxCacheDiskWrites(value: string): SizeResult {
  if (value.trim() === '0') return { ok: true, bytes: 0 };
  const r = parseMaxMemory(value);
  return r.ok && r.bytes !== null ? r : { ok: false, error: 'use 0 for no limit, or a size such as 200G' };
}

/** `--max-request-size`: a positive byte count such as 128M. */
export function parseRequestSize(value: string): SizeResult {
  const size = value.trim().toUpperCase() === 'AUTO' ? null : parseBytes(value);
  return size === null
    ? { ok: false, error: 'must be a positive byte count such as 128M' }
    : { ok: true, bytes: Number(size) };
}

/** `_memory_size`: 'auto' (null), 'off' where the option has it (0 here: the store loads whole),
 * or a size. Splash refuses a size below the store's smallest when it starts. */
function parseStoreMemory(value: string, example: string, off: boolean): SizeResult {
  const normalized = value.trim().toLowerCase();
  if (normalized === 'auto') return { ok: true, bytes: null };
  if (off && normalized === 'off') return { ok: true, bytes: 0 };
  const r = parseMaxMemory(value);
  return r.ok ? r : { ok: false, error: `use ${off ? 'auto, off' : 'auto'}, or a size such as ${example}` };
}

/** `--expert-memory`: auto, off (every expert loads) or a size such as 16G. */
export function parseExpertMemory(value: string): SizeResult {
  return parseStoreMemory(value, '16G', true);
}

/** `--ple-memory`: auto, off (the whole PLE table loads) or a size such as 2G. */
export function parsePleMemory(value: string): SizeResult {
  return parseStoreMemory(value, '2G', true);
}

/** `--sparse-kv-memory`: auto or a size such as 1200M; it has no off. */
export function parseSparseKvMemory(value: string): SizeResult {
  return parseStoreMemory(value, '1200M', false);
}

/** splash/server/serve_options.py MAX_CONTEXT_TOKENS: the native window (256K). */
export const MAX_CONTEXT_TOKENS = 262144;
/** MAX_YARN_CONTEXT_TOKENS: the most --max-context takes; past the native window it turns on YaRN. */
export const MAX_YARN_CONTEXT_TOKENS = 1010000;
const CONTEXT_UNITS: Record<string, number> = { K: 1024, M: 1000000 };

export type TokensResult = { ok: true; tokens: number | null } | { ok: false; error: string };

/** `--max-context`: 'auto' (null) or a token count up to 1,010,000; K is 1024 tokens, M a million. */
export function parseMaxContext(value: string): TokensResult {
  const normalized = value.trim().toUpperCase();
  if (normalized === 'AUTO') return { ok: true, tokens: null };
  const unit = CONTEXT_UNITS[normalized.slice(-1)];
  const digits = unit ? normalized.slice(0, -1) : normalized;
  const tokens = PY_INT.test(digits) ? Number(digits.trim().replace(/_/g, '')) * (unit ?? 1) : 0;
  return tokens >= 1 && tokens <= MAX_YARN_CONTEXT_TOKENS
    ? { ok: true, tokens }
    : { ok: false, error: "must be 'auto' or a token count up to 1010000, such as 100K or 1M (K = 1024 tokens, M = 1000000 tokens)" };
}

export type SizeKind = 'max-memory' | 'max-cache-disk' | 'max-cache-disk-writes' | 'request-size' | 'expert-memory' | 'ple-memory' | 'sparse-kv-memory';

export function parseSize(kind: SizeKind, value: string): SizeResult {
  switch (kind) {
    case 'max-memory':
      return parseMaxMemory(value);
    case 'max-cache-disk':
      return parseMaxCacheDisk(value);
    case 'max-cache-disk-writes':
      return parseMaxCacheDiskWrites(value);
    case 'request-size':
      return parseRequestSize(value);
    case 'expert-memory':
      return parseExpertMemory(value);
    case 'ple-memory':
      return parsePleMemory(value);
    case 'sparse-kv-memory':
      return parseSparseKvMemory(value);
  }
}
