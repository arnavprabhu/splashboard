import { expect, test } from '../support/manager';

test.describe('shell against the real manager', () => {
  test('nav shows the fake engine version and a stopped chip', async ({ page }) => {
    await page.goto('/admin/status');
    await expect(page.getByRole('navigation', { name: 'Main' })).toBeVisible();
    await expect(page.locator('.navband').getByText('Splash 1.2.0')).toBeVisible();
    await expect(page.locator('.navband .chip')).toHaveAttribute('data-live', 'false');
  });

  test('theme toggle flips and persists without a flash', async ({ page }) => {
    await page.goto('/admin/status');
    const html = page.locator('html');
    await expect(html).toHaveAttribute('data-theme', 'light');
    await page.getByTestId('theme-toggle').click();
    await expect(html).toHaveAttribute('data-theme', 'dark');
    await page.reload();
    await expect(html).toHaveAttribute('data-theme', 'dark');
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    expect(bg).toBe('rgb(14, 14, 14)');
  });

  test('g then m navigates and ? opens the shortcuts sheet', async ({ page }) => {
    await page.goto('/admin/status');
    await page.locator('body').click();
    await page.keyboard.press('g');
    await page.keyboard.press('m');
    await expect(page).toHaveURL(/\/admin\/models$/);
    await page.keyboard.press('?');
    await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts.' })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
  });
});
