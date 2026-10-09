/**
 * Tokenizer helpers.
 *
 * Splash's `/tokenize` returns IDs only (`with_pieces` is refused; splash/server/frontend.py
 * `tokenize`), and there is no detokenize route. Pieces are reconstructed from the source text
 * with batched `/tokenize` calls: the text is cut into small candidate segments, each unique
 * segment is tokenized on its own, and the concatenation is verified against the tokenization
 * of the whole text. Where a segment does not line up it is merged with the next one and tried
 * again; segments that hold several tokens are split by tokenizing their prefixes. If the
 * result cannot be verified, the view falls back to IDs only (Q6's fallback).
 */

export type TokenizeFn = (text: string, addSpecial: boolean) => Promise<number[]>;

export interface TokenPiece {
  id: number;
  /** The text this token covers; null when unknown (IDs only, or an added special token). */
  piece: string | null;
  /** True for special tokens (`<|im_start|>`, `<think>`, BOS/EOS added by add_special). */
  special: boolean;
  /** Continuation of a multi-token span whose text could not be split (e.g. one emoji). */
  joined?: boolean;
}

export type PiecesResult =
  | { ok: true; tokens: TokenPiece[]; requests: number }
  | { ok: false; reason: 'mismatch' | 'too_long' | 'error'; tokens: TokenPiece[]; requests: number };

/** Special-token spellings recognised in text (`/tokenize` parses them, parse_special=true). */
export const SPECIAL_PATTERN = /<\|[^|>\s]+\|>|<\/?think>|<\/?tool_call>|<\/?tool_response>|<\/?s>/g;

export function isSpecialText(piece: string): boolean {
  SPECIAL_PATTERN.lastIndex = 0;
  const m = SPECIAL_PATTERN.exec(piece);
  return !!m && m[0] === piece;
}

/**
 * Candidate segments: special tokens, then runs split where byte-level BPE pre-tokenizers
 * usually split (a leading space joins the following letters/punctuation; digits stand alone;
 * whitespace runs are their own segment). The verification step makes the exact rule irrelevant.
 */
export function candidateSegments(text: string): string[] {
  const out: string[] = [];
  let last = 0;
  SPECIAL_PATTERN.lastIndex = 0;
  for (let m = SPECIAL_PATTERN.exec(text); m; m = SPECIAL_PATTERN.exec(text)) {
    if (m.index > last) out.push(...plainSegments(text.slice(last, m.index)));
    out.push(m[0]);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(...plainSegments(text.slice(last)));
  return out;
}

const PLAIN = /'(?:s|t|re|ve|m|ll|d)\b|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+/gu;

function plainSegments(text: string): string[] {
  return text.match(PLAIN) ?? [text];
}

function eq(a: readonly number[], b: readonly number[], offset = 0): boolean {
  if (offset + a.length > b.length) return false;
  for (let i = 0; i < a.length; i += 1) if (a[i] !== b[offset + i]) return false;
  return true;
}

/** Runs `fn` over `items` with at most `limit` in flight. */
async function pool<T, R>(items: readonly T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out = new Array<R>(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i]!);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

export interface PieceOptions {
  /** Unique segments above this are not reconstructed (cost guard). */
  maxSegments?: number;
  concurrency?: number;
  /** A merged segment longer than this gives up (the tokenizer joins across too much text). */
  maxSegmentChars?: number;
}

/**
 * Reconstructs each token's text. `full` is `/tokenize` of the whole text with `addSpecial`;
 * the base alignment uses `add_special: false`, and extra tokens at either end are marked as
 * special tokens the tokenizer added.
 */
export async function reconstructPieces(text: string, full: readonly number[], addSpecial: boolean, tokenize: TokenizeFn, options: PieceOptions = {}): Promise<PiecesResult> {
  const { maxSegments = 400, concurrency = 8, maxSegmentChars = 400 } = options;
  const idsOnly = (): TokenPiece[] => full.map((id) => ({ id, piece: null, special: false }));
  let requests = 0;
  const cache = new Map<string, Promise<number[]>>();
  const tok = (s: string): Promise<number[]> => {
    let p = cache.get(s);
    if (!p) {
      requests += 1;
      p = tokenize(s, false);
      cache.set(s, p);
    }
    return p;
  };
  try {
    let base = full;
    if (addSpecial) base = await tok(text);
    // Tokens the tokenizer added around the text (BOS/EOS).
    let lead = 0;
    if (addSpecial) {
      let found = -1;
      for (let o = 0; o + base.length <= full.length; o += 1) {
        if (eq(base, full, o)) {
          found = o;
          break;
        }
      }
      if (found < 0) return { ok: false, reason: 'mismatch', tokens: idsOnly(), requests };
      lead = found;
    }
    const segments = candidateSegments(text);
    if (new Set(segments).size > maxSegments) return { ok: false, reason: 'too_long', tokens: idsOnly(), requests };
    await pool([...new Set(segments)], concurrency, tok);

    // Align segments against the base tokenization, merging forward on a mismatch.
    const spans: Array<{ text: string; ids: number[] }> = [];
    let p = 0;
    let i = 0;
    while (i < segments.length) {
      let s = segments[i]!;
      let j = i + 1;
      let ids = await tok(s);
      while (!eq(ids, base, p) && j < segments.length) {
        s += segments[j]!;
        j += 1;
        if (s.length > maxSegmentChars) return { ok: false, reason: 'mismatch', tokens: idsOnly(), requests };
        ids = await tok(s);
      }
      if (!eq(ids, base, p)) return { ok: false, reason: 'mismatch', tokens: idsOnly(), requests };
      spans.push({ text: s, ids });
      p += ids.length;
      i = j;
    }
    if (p !== base.length) return { ok: false, reason: 'mismatch', tokens: idsOnly(), requests };

    // Split multi-token spans by tokenizing prefixes at each character boundary.
    const pieces: TokenPiece[] = [];
    for (const span of spans) {
      if (span.ids.length === 1) {
        pieces.push({ id: span.ids[0]!, piece: span.text, special: isSpecialText(span.text) });
        continue;
      }
      if (span.ids.length === 0) continue;
      const chars = Array.from(span.text);
      const cuts: number[] = [];
      let k = 0;
      let offset = 0;
      for (let c = 1; c < chars.length && k < span.ids.length - 1; c += 1) {
        offset += chars[c - 1]!.length;
        const prefix = await tok(span.text.slice(0, offset));
        if (prefix.length === k + 1 && eq(prefix, span.ids)) {
          cuts.push(offset);
          k += 1;
        }
      }
      let start = 0;
      for (let t = 0; t < span.ids.length; t += 1) {
        const end = t < cuts.length ? cuts[t]! : t === span.ids.length - 1 ? span.text.length : -1;
        if (end < 0) {
          // No clean character boundary (a token ends mid-character): the remaining text goes on
          // this token and the rest are marked as joined continuations.
          pieces.push({ id: span.ids[t]!, piece: span.text.slice(start), special: false });
          for (let r = t + 1; r < span.ids.length; r += 1) pieces.push({ id: span.ids[r]!, piece: '', special: false, joined: true });
          start = span.text.length;
          break;
        }
        const piece = span.text.slice(start, end);
        pieces.push({ id: span.ids[t]!, piece, special: isSpecialText(piece) });
        start = end;
      }
    }
    const added = (id: number): TokenPiece => ({ id, piece: null, special: true });
    const tokens = [...full.slice(0, lead).map(added), ...pieces, ...full.slice(lead + base.length).map(added)];
    return { ok: true, tokens, requests };
  } catch {
    return { ok: false, reason: 'error', tokens: idsOnly(), requests };
  }
}

/** Whitespace made visible for chips: ␣ for spaces, ⏎ for newlines, → for tabs. */
export function visibleWhitespace(piece: string): string {
  return piece.replace(/ /g, '␣').replace(/\n/g, '⏎').replace(/\t/g, '→').replace(/\r/g, '');
}

export function utf8Length(s: string): number {
  return new TextEncoder().encode(s).length;
}

// ---------- rendered template highlighting ----------

export interface PromptPart {
  text: string;
  special: boolean;
}

/** Splits a rendered prompt into plain text and special-token spans. */
export function splitSpecial(prompt: string): PromptPart[] {
  const out: PromptPart[] = [];
  let last = 0;
  SPECIAL_PATTERN.lastIndex = 0;
  for (let m = SPECIAL_PATTERN.exec(prompt); m; m = SPECIAL_PATTERN.exec(prompt)) {
    if (m.index > last) out.push({ text: prompt.slice(last, m.index), special: false });
    out.push({ text: m[0], special: true });
    last = m.index + m[0].length;
  }
  if (last < prompt.length) out.push({ text: prompt.slice(last), special: false });
  return out;
}

export function countSpecial(prompt: string): number {
  return splitSpecial(prompt).filter((p) => p.special).length;
}
