import { describe, expect, it } from 'vitest';
import { en, t } from '../src/strings/en';
import { format, makeT } from '../src/strings/index';

// Every area module: keys must start with the area prefix and be unique across the table.
const modules = import.meta.glob<Record<string, unknown>>('../src/strings/*.ts', { eager: true });

describe('format', () => {
  it('fills placeholders and leaves unknown ones', () => {
    expect(format('{model} downloaded.', { model: 'a/b' })).toBe('a/b downloaded.');
    expect(format('{a} and {b}', { a: 'x' })).toBe('x and {b}');
    expect(format('{n} files', { n: 1234 })).toBe('1,234 files');
  });
});

describe('makeT', () => {
  const tt = makeT({ 'x.count.one': 'Delete {n} model', 'x.count.other': 'Delete {n} models', 'x.plain': 'Plain' } as const);
  it('selects plurals by n', () => {
    expect(tt('x.count', { n: 1 })).toBe('Delete 1 model');
    expect(tt('x.count', { n: 3 })).toBe('Delete 3 models');
    expect(tt('x.count', { n: 0 })).toBe('Delete 0 models');
  });
  it('looks up plain keys and falls back to the key', () => {
    expect(tt('x.plain')).toBe('Plain');
    expect((tt as (k: string) => string)('x.missing')).toBe('x.missing');
  });
  it('common table works', () => {
    expect(t('common.copy')).toBe('Copy');
    expect(t('alert.more', { n: 2 })).toBe('+2 more ▾');
  });
});

describe('string table', () => {
  it('keeps keys unique and prefixed per area module', () => {
    const seen = new Map<string, string>();
    for (const key of Object.keys(en)) seen.set(key, 'en');
    for (const [path, mod] of Object.entries(modules)) {
      const area = path.split('/').pop()!.replace(/\.ts$/, '');
      if (area === 'en' || area === 'index') continue;
      const table = mod[`${area}Strings`] as Record<string, string> | undefined;
      expect(table, `${path} exports ${area}Strings`).toBeTruthy();
      for (const key of Object.keys(table!)) {
        expect(key.startsWith(`${area}.`), `${key} in ${path} starts with ${area}.`).toBe(true);
        expect(seen.has(key), `${key} defined twice (${seen.get(key)} and ${area})`).toBe(false);
        seen.set(key, area);
      }
    }
  });

  it('never types uppercase into user-visible strings for CSS to handle', () => {
    // docs/ui/00 §3: uppercase comes from CSS; words like ON/OFF/DELETE are the exceptions.
    const allowed = /^(ON|OFF|DELETE|CLEAR|API|URL|SSD|RAM|GPU|MEM|KV|HF|MLX|GGUF|ID|CLI|JSON|PDF|CSV|TTFT|ITL|MCP|SDK|LAN|AC|OK|PID|CPU|DMG|URL|HTTP|HTTPS|SSE|GB|MB|KB|TB|K|M|G|B|UI|PATH|BF16|MiB|GiB|AA|GUI)$/;
    for (const [key, value] of Object.entries(en)) {
      const words = value.match(/\b[A-Z]{3,}\b/g) ?? [];
      for (const w of words) expect(allowed.test(w), `${key}: "${w}"`).toBe(true);
    }
  });
});

describe('placeholders at call sites', () => {
  it('every t(key, {…}) call passes each {placeholder} its template uses', async () => {
    // Regression: `settings.auto_for` read "Auto ({value})" while every caller passed
    // {label}, so the Auto toggles' accessible names were literally "Auto ({value})".
    const ts = (await import('typescript')).default;
    const { readFileSync, readdirSync, statSync } = await import('node:fs');
    const { join, dirname } = await import('node:path');
    const { fileURLToPath } = await import('node:url');
    const table: Record<string, string> = { ...en };
    for (const [path, mod] of Object.entries(modules)) {
      const area = path.split('/').pop()!.replace(/\.ts$/, '');
      Object.assign(table, (mod[`${area}Strings`] as Record<string, string> | undefined) ?? {});
    }
    const src = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        if (statSync(full).isDirectory()) walk(full);
        else if (/\.tsx?$/.test(name)) files.push(full);
      }
    };
    walk(src);
    const problems: string[] = [];
    let checked = 0;
    for (const file of files) {
      const sf = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true);
      const visit = (node: import('typescript').Node) => {
        const [first, second] = ts.isCallExpression(node) ? node.arguments : [];
        if (
          ts.isCallExpression(node) &&
          ts.isIdentifier(node.expression) &&
          node.expression.text === 't' &&
          first !== undefined &&
          ts.isStringLiteralLike(first) &&
          (node.arguments.length === 1 || (node.arguments.length === 2 && second !== undefined && ts.isObjectLiteralExpression(second)))
        ) {
          const key = first.text;
          const params = second as import('typescript').ObjectLiteralExpression | undefined;
          if (params?.properties.some((p) => ts.isSpreadAssignment(p))) return;
          // `t('login.help').split('{cmd}')` puts markup in the placeholder on purpose.
          const parent = node.parent;
          if (!params && ts.isPropertyAccessExpression(parent) && parent.name.text === 'split') return;
          const passed = new Set((params?.properties ?? []).map((p) => (p.name && (ts.isIdentifier(p.name) || ts.isStringLiteral(p.name)) ? p.name.text : '')));
          const templates = [table[key], table[`${key}.one`], table[`${key}.other`]].filter((v): v is string => v !== undefined);
          for (const template of templates) {
            checked++;
            for (const [, name = ''] of template.matchAll(/\{(\w+)\}/g)) {
              if (!passed.has(name)) problems.push(`${file.slice(src.length)}: t('${key}') uses {${name}}, passes {${[...passed].join(', ')}}`);
            }
          }
        }
        ts.forEachChild(node, visit);
      };
      visit(sf);
    }
    expect(checked).toBeGreaterThan(50);
    expect(problems).toEqual([]);
  });
});
