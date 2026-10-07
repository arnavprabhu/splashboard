import { FAKE_MODEL, expect, test } from '../support/manager';

// Splash 1.3.0's /status blocks (runtime/engine/Status.cpp: weights, ane_ffn) through the real
// manager and the fake engine. FAKE_MODEL is the 35B MoE, which the Neural Engine split never takes.
test.describe('status bands against the real manager', () => {
  test.use({ managerOptions: { installed: [FAKE_MODEL] } });

  test('Neural Engine band reads the MoE model as GPU only; Memory shows the weights', async ({ page, manager }) => {
    const load = await manager.api('POST', '/engine/load', { model: FAKE_MODEL });
    expect(load.status, JSON.stringify(load.body)).toBeLessThan(300);
    await expect
      .poll(async () => (await manager.api<{ state: string }>('GET', '/engine')).body.state, { timeout: 30_000 })
      .toBe('ready');
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/admin/status');
    const band = page.locator('#neural-engine');
    await expect(band.getByText('Off · GPU only')).toBeVisible();
    await expect(band.getByText('the target has no dense FFN layers')).toBeVisible();
    await expect(band.getByText('Share')).toHaveCount(0);
    await expect(band.locator('[data-accent="true"]')).toHaveCount(0);
    await expect(page.locator('#memory').getByText('in memory')).toBeVisible();
    await expect(page.locator('#memory').getByText(/released after 10 m idle/)).toBeVisible();
  });
});
