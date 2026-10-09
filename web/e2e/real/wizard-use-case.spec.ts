/**
 * Welcome wizard step 3 on a real manager (SPEC §10.2, §8.2; D78): the choice is saved as
 * `wizard.use_case` the moment it is picked, so a reload or another browser resumes it, while
 * `wizard.preset` (the applied one) only changes when Continue applies it.
 */
import { expect, test } from '../support/manager';

test.use({ managerOptions: { wizardCompleted: false } });

async function wizard(manager: { api: (m: string, p: string) => Promise<{ body: unknown }> }) {
  const r = await manager.api('GET', '/settings');
  return (r.body as { settings: { global: { wizard: Record<string, unknown> } } }).settings.global.wizard;
}

test('step 3 saves the pick as use_case until Continue applies it, and a reload resumes the pick', async ({ page, manager }) => {
  await page.goto('/admin/welcome?step=3');
  await expect(page.getByText('Step 3 of 5', { exact: false })).toBeVisible();
  await page.getByText('Max speed', { exact: true }).click();
  await expect.poll(() => wizard(manager)).toMatchObject({ use_case: 'speed', preset: null });

  await page.reload();
  await expect(page.locator('.wz-preset[data-selected="true"]')).toContainText('Max speed');
  expect((await wizard(manager)).preset).toBeNull();

  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByText('Step 4 of 5', { exact: false })).toBeVisible();
  await expect.poll(() => wizard(manager)).toMatchObject({ preset: 'speed', use_case: null });
});
