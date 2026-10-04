#!/usr/bin/env node
// Bundle budget check (SPEC §18.6). Run after `pnpm build`.
//   initial JS for the Status route <= 60 KB gzip
//   Chat route JS beyond the initial set <= 80 KB gzip
//   total CSS <= 20 KB gzip
//   font files <= 90 KB each
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const dist = process.env.SPLASH_GUI_DIST ? join(root, process.env.SPLASH_GUI_DIST) : join(root, 'dist');
const manifestPath = join(dist, '.vite', 'manifest.json');
const KB = 1024;
const BUDGETS = { initialJs: 60 * KB, chatExtra: 80 * KB, css: 20 * KB, font: 90 * KB };

if (!existsSync(manifestPath)) {
  console.error('dist/.vite/manifest.json not found. Run `pnpm build` first.');
  process.exit(2);
}
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const gz = (file) => gzipSync(readFileSync(join(dist, file)), { level: 9 }).length;
const kb = (n) => `${(n / KB).toFixed(1)} KB`;

/** Static closure of JS files for the given manifest keys. */
function closure(keys) {
  const files = new Set();
  const seen = new Set();
  const visit = (key) => {
    if (seen.has(key)) return;
    seen.add(key);
    const chunk = manifest[key];
    if (!chunk) throw new Error(`manifest has no entry for ${key}`);
    if (chunk.file.endsWith('.js')) files.add(chunk.file);
    for (const dep of chunk.imports ?? []) visit(dep);
  };
  keys.forEach(visit);
  return files;
}

const entryKey = Object.keys(manifest).find((k) => manifest[k].isEntry);
const routeKey = (name) => {
  const key = Object.keys(manifest).find((k) => k === `src/routes/${name}.tsx`);
  if (!key) throw new Error(`route chunk src/routes/${name}.tsx missing from manifest`);
  return key;
};

const initial = closure([entryKey, routeKey('status')]);
const initialJs = [...initial].reduce((sum, f) => sum + gz(f), 0);
const chatAll = closure([entryKey, routeKey('chat')]);
const chatExtra = [...chatAll].filter((f) => !initial.has(f)).reduce((sum, f) => sum + gz(f), 0);

const assets = readdirSync(join(dist, 'assets'));
const css = assets.filter((f) => f.endsWith('.css')).reduce((sum, f) => sum + gz(join('assets', f)), 0);
const fonts = assets
  .filter((f) => f.endsWith('.woff2'))
  .map((f) => ({ f, size: readFileSync(join(dist, 'assets', f)).length }));

const rows = [
  ['Initial JS (Status route)', initialJs, BUDGETS.initialJs],
  ['Chat route extra JS', chatExtra, BUDGETS.chatExtra],
  ['Total CSS', css, BUDGETS.css],
  ...fonts.map(({ f, size }) => [`Font ${f}`, size, BUDGETS.font]),
];

let failed = false;
for (const [label, size, budget] of rows) {
  const ok = size <= budget;
  failed ||= !ok;
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label.padEnd(44)} ${kb(size).padStart(9)} / ${kb(budget)}`);
}
if (fonts.length === 0) {
  console.log('FAIL  no font files in dist/assets');
  failed = true;
}
const uplot = Object.values(manifest).find((c) => /uPlot/i.test(c.file));
if (uplot) console.log(`info  Status charts add uPlot lazily: ${kb(gz(uplot.file))} (initial + uPlot = ${kb(initialJs + gz(uplot.file))})`);
console.log(`\nInitial set: ${[...initial].join(', ')}`);
process.exit(failed ? 1 : 0);
