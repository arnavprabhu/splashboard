import { describe, expect, it } from 'vitest';
import { en } from '../src/strings/en';

// Every `t('key', { … })` call in the app must fill a template that names each param, and every
// `{placeholder}` in a template must be supplied (docs/ui/00 §8.2). Stub copy such as
// "models.disk.models": "Models" called with `{ size }` dropped the value from the page.
const modules = import.meta.glob<Record<string, unknown>>('../src/strings/*.ts', { eager: true });
const sources = import.meta.glob<string>(['../src/**/*.ts', '../src/**/*.tsx', '!../src/strings/**'], {
  eager: true,
  query: '?raw',
  import: 'default',
});

const table: Record<string, string> = { ...en };
for (const [path, mod] of Object.entries(modules)) {
  const area = path.split('/').pop()!.replace(/\.ts$/, '');
  const strings = mod[`${area}Strings`] as Record<string, string> | undefined;
  if (strings) Object.assign(table, strings);
}

/** Top-level property names of an object literal body (`a: 1, b, c: f(x, y)`). */
function topLevelKeys(body: string): string[] {
  const keys: string[] = [];
  let depth = 0;
  let part = '';
  for (const ch of body + ',') {
    if ('{([`'.includes(ch)) depth += 1;
    if ('})]'.includes(ch)) depth -= 1;
    if (ch === ',' && depth === 0) {
      const m = /^\s*(?:\.\.\.)?([A-Za-z_$][\w$]*)\s*(?::|$)/.exec(part);
      if (m && !part.trim().startsWith('...')) keys.push(m[1]!);
      part = '';
    } else part += ch;
  }
  return keys;
}

interface Call {
  where: string;
  key: string;
  params: string[] | null;
}

function calls(): Call[] {
  const out: Call[] = [];
  // Single- and double-quoted keys (the route modules use double quotes).
  const re = /\bt\(\s*['"]([a-z0-9_.]+)['"]\s*(?:as\s+[^,)]+)?\s*(,\s*\{)?/g;
  for (const [path, text] of Object.entries(sources)) {
    for (const m of text.matchAll(re)) {
      let params: string[] | null = [];
      if (m[2]) {
        let i = m.index! + m[0].length;
        let depth = 1;
        const start = i;
        while (i < text.length && depth > 0) {
          if (text[i] === '{') depth += 1;
          else if (text[i] === '}') depth -= 1;
          i += 1;
        }
        const body = text.slice(start, i - 1);
        params = body.includes('...') ? null : topLevelKeys(body);
      }
      const line = text.slice(0, m.index).split('\n').length;
      out.push({ where: `${path.replace('../', '')}:${line}`, key: m[1]!, params });
    }
  }
  return out;
}

function templates(key: string): string[] {
  if (key in table) return [table[key]!];
  return [`${key}.one`, `${key}.other`].filter((k) => k in table).map((k) => table[k]!);
}

describe('string params', () => {
  const all = calls();

  it('finds the calls', () => {
    expect(all.length).toBeGreaterThan(500);
  });

  it('every param a call passes appears in its template', () => {
    const problems: string[] = [];
    for (const c of all) {
      const tpls = templates(c.key);
      if (!tpls.length || !c.params) continue;
      const plural = !(c.key in table);
      for (const p of c.params) {
        if (plural && p === 'n') continue;
        if (!tpls.some((tpl) => tpl.includes(`{${p}}`))) problems.push(`${c.where} ${c.key}: {${p}} not in "${tpls[0]}"`);
      }
    }
    expect(problems).toEqual([]);
  });

  it('every placeholder in a template is passed', () => {
    const problems: string[] = [];
    for (const c of all) {
      if (!c.params) continue;
      for (const tpl of templates(c.key)) {
        for (const [, name] of tpl.matchAll(/\{(\w+)\}/g)) {
          if (!c.params.includes(name!)) problems.push(`${c.where} ${c.key}: "${tpl}" needs {${name}}`);
        }
      }
    }
    expect([...new Set(problems)]).toEqual([]);
  });
});
