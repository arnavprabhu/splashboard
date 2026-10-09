/**
 * Tools → Benchmark → Saved runs: a long model ID stays on one line, truncated in the middle,
 * with the full ID as its tooltip.
 */
import { expect, test } from '@playwright/test';

import { USAGE, V1_MODELS, mockManager } from './fixtures';

const LONG = 'local/OrcaSAQ-2-27B-Uncensored-GGUF';
const RUN = { id: 'r1', ts: '2026-10-08T09:30:00Z', state: 'done', model: LONG, engine_version: '1.3.0', headline: { decode_tps: 41.2 } };

test('saved runs show a long model ID on one line with its full ID in the title', async ({ page }) => {
  await mockManager(page, {
    usage: USAGE,
    extra: (_m, p) => {
      if (p === '/benchmark/runs') return { json: { runs: [RUN] } };
      if (p === '/benchmark/preflight') return { json: { ready: true, warnings: [] } };
      return undefined;
    },
  });
  await page.route('**/v1/models', (r) => r.fulfill({ json: V1_MODELS }));
  await page.goto('/admin/tools/benchmark');
  const name = page.locator('.mid-trunc').first();
  await expect(name).toBeVisible();
  await expect(name).toHaveAttribute('title', LONG);
  // Screen readers and copy still get the whole name.
  await expect(name).toHaveText('OrcaSAQ-2-27B-Uncensored-GGUF');
  const m = await name.evaluate((el) => {
    const lh = parseFloat(getComputedStyle(el).lineHeight) || parseFloat(getComputedStyle(el).fontSize) * 1.5;
    const head = el.querySelector('.mid-trunc-head') as HTMLElement;
    return { height: el.getBoundingClientRect().height, lh, tail: el.querySelector('.mid-trunc-tail')!.getBoundingClientRect().width, headTruncated: head.scrollWidth > head.clientWidth };
  });
  expect(m.height).toBeLessThan(m.lh * 1.5);
  expect(m.tail).toBeGreaterThan(0);
  // Nothing on the page scrolls sideways at phone width because of the name.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
