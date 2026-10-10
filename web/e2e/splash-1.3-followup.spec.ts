/**
 * The plain MLX refusal with Splash's words as the detail (Splash 1.3.1 wording).
 */
import { expect, test } from '@playwright/test';

import { mockManager } from './fixtures';

const SEVEN_BIT = 'mlx-community/Qwen3.8-27B-7bit';
const PLAIN = 'Splash runs MLX models quantized as affine 2, 3, 4, 5, 6 or 8 bits (groups of 32, 64 or 128) or as mxfp4.';
const DETAIL =
  'quantization language_model.model.embed_tokens is affine 7-bit in groups of 64; MLX weights load as affine 2, 3, 4, 5, 6 or 8 bits in groups of 32, 64 or 128, or as mxfp4';

test('A 7-bit MLX checkpoint leads with the plain line and keeps Splash’s reason as detail', async ({ page }) => {
  await mockManager(page, {
    extra: (_m, path) => {
      if (path === '/inspect')
        return {
          json: {
            id: SEVEN_BIT,
            repo_id: SEVEN_BIT,
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
  await page.goto(`/admin/models/downloader?tab=id&id=${SEVEN_BIT}`);
  await expect(page.getByTestId('compat-reason')).toHaveText(PLAIN);
  await expect(page.getByTestId('compat-detail')).toHaveText(DETAIL);
});
