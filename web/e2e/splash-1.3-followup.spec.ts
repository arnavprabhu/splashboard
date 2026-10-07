/**
 * Splash 1.3.0 follow-up (2026-10-07): the plain MLX refusal with Splash's words as the detail
 * (D53).
 */
import { expect, test } from '@playwright/test';

import { mockManager } from './fixtures';

const EIGHT_BIT = 'mlx-community/Qwen3.8-27B-8bit';
const PLAIN = 'Splash runs MLX models only at 4-bit, group size 64.';
const DETAIL = 'quantization language_model.model.embed_tokens bits mismatch: MLX 8, runtime 4';

test('D53: an 8-bit MLX checkpoint leads with the plain line and keeps Splash’s reason as detail', async ({ page }) => {
  await mockManager(page, {
    extra: (_m, path) => {
      if (path === '/inspect')
        return {
          json: {
            id: EIGHT_BIT,
            repo_id: EIGHT_BIT,
            compatible: false,
            badge: 'incompatible',
            family: null,
            format: null,
            variants: [],
            vision: { available: false, reason: PLAIN },
            reason: PLAIN,
            reason_detail: DETAIL,
            cached: false,
            checked_at: '',
          },
        };
      return undefined;
    },
  });
  await page.goto(`/admin/models/downloader?tab=id&id=${EIGHT_BIT}`);
  await expect(page.getByTestId('compat-reason')).toHaveText(PLAIN);
  await expect(page.getByTestId('compat-detail')).toHaveText(DETAIL);
});
