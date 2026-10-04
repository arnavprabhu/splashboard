/**
 * The JSON editor's logic (SPEC §18.6: a textarea with validation and indentation helpers,
 * no CodeMirror). Pure functions so they are unit-tested without a DOM.
 */

export type JsonCheck =
  | { ok: true; value: unknown }
  | { ok: false; message: string; line: number; col: number };

/** 1-based line and column of a character offset. */
export function lineCol(text: string, offset: number): { line: number; col: number } {
  const before = text.slice(0, Math.max(0, Math.min(offset, text.length)));
  const lines = before.split('\n');
  return { line: lines.length, col: (lines[lines.length - 1]?.length ?? 0) + 1 };
}

/**
 * Parses JSON and, on failure, reports where. V8 messages carry either "(line L column C)"
 * or "at position N"; other engines fall back to the end of the text.
 */
export function checkJson(text: string): JsonCheck {
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch (err) {
    const raw = err instanceof Error ? err.message : String(err);
    const lc = /line (\d+) column (\d+)/.exec(raw);
    let line: number;
    let col: number;
    if (lc) {
      line = Number(lc[1]);
      col = Number(lc[2]);
    } else {
      const pos = /position (\d+)/.exec(raw);
      ({ line, col } = lineCol(text, pos ? Number(pos[1]) : text.length));
    }
    const message = raw
      .replace(/^JSON\.parse: /, '')
      .replace(/ in JSON at position \d+.*$/, '')
      .replace(/ \(line \d+ column \d+\)$/, '')
      .replace(/,? "[^"]*"\.\.\. is not valid JSON$/, '');
    return { ok: false, message: message || 'Invalid JSON', line, col };
  }
}

export function formatJson(text: string): string | null {
  const c = checkJson(text);
  return c.ok ? JSON.stringify(c.value, null, 2) : null;
}

export interface Edit {
  text: string;
  /** Caret after the edit. */
  caret: number;
}

const INDENT = '  ';

/** Tab: indents at the caret (or every selected line); Shift+Tab outdents the selected lines. */
export function indent(text: string, start: number, end: number, outdent = false): Edit & { selStart: number; selEnd: number } {
  if (start === end && !outdent) {
    const t = text.slice(0, start) + INDENT + text.slice(end);
    return { text: t, caret: start + INDENT.length, selStart: start + INDENT.length, selEnd: start + INDENT.length };
  }
  const lineStart = text.lastIndexOf('\n', start - 1) + 1;
  const block = text.slice(lineStart, end);
  const lines = block.split('\n');
  let delta0 = 0;
  let total = 0;
  const changed = lines.map((l, i) => {
    if (outdent) {
      const n = l.startsWith(INDENT) ? 2 : l.startsWith(' ') ? 1 : 0;
      if (i === 0) delta0 = -n;
      total -= n;
      return l.slice(n);
    }
    if (i === 0) delta0 = INDENT.length;
    total += INDENT.length;
    return INDENT + l;
  });
  const t = text.slice(0, lineStart) + changed.join('\n') + text.slice(end);
  const selStart = Math.max(lineStart, start + delta0);
  const selEnd = end + total;
  return { text: t, caret: selEnd, selStart, selEnd };
}

/** Enter: keeps the line's indentation, adds a level after `{` or `[`, splits `{}` / `[]` pairs. */
export function newline(text: string, start: number, end: number): Edit {
  const lineStart = text.lastIndexOf('\n', start - 1) + 1;
  const current = /^[ \t]*/.exec(text.slice(lineStart, start))?.[0] ?? '';
  const before = text.slice(0, start).trimEnd();
  const after = text.slice(end);
  const opens = /[[{]$/.test(before);
  const closesNext = /^\s*[\]}]/.test(after) && opens;
  if (opens && closesNext) {
    const inner = `\n${current}${INDENT}`;
    const t = `${text.slice(0, start)}${inner}\n${current}${after.replace(/^[ \t]*/, '')}`;
    return { text: t, caret: start + inner.length };
  }
  const insert = `\n${current}${opens ? INDENT : ''}`;
  return { text: text.slice(0, start) + insert + after, caret: start + insert.length };
}
