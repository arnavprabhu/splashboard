/**
 * Welcome wizard step 2 on a manager started with `--port` (acceptance 1.3 F2): the e2e
 * harness starts every manager with `--port <free>` while `server.port` keeps its default
 * 8000, where oMLX lives on the owner's Mac. Step 2 must call the port the manager listens
 * on current, and must refuse a port another process holds.
 */
import { createServer, type Server } from 'node:net';
import { expect, test } from '../support/manager';

test.use({ managerOptions: { wizardCompleted: false } });

test('step 2 shows the listening port as current and refuses a busy one', async ({ page, manager }) => {
  const settings = await manager.api<{ listening_port: number; settings: { global: { server: { port: number } } } }>('GET', '/settings');
  expect(settings.body.listening_port).toBe(manager.port);
  expect(settings.body.settings.global.server.port).not.toBe(manager.port);

  await page.addInitScript(() => {
    localStorage.setItem('splash-gui-wizard', JSON.stringify({ step: 2, reached: 2, pendingPort: null, preset: null, model: null, downloadId: null }));
  });
  await page.goto('/admin/welcome');
  await expect(page.getByText('Step 2 of 5', { exact: false })).toBeVisible();
  const field = page.getByTestId('field-port');
  await expect(page.getByLabel('Server port')).toHaveValue(String(manager.port));
  await expect(page.getByTestId('port-status')).toHaveText('Current manager port');
  await expect(field).toContainText(`http://127.0.0.1:${manager.port}`);

  const busy: Server = createServer();
  await new Promise<void>((ok) => busy.listen(0, '127.0.0.1', () => ok()));
  const taken = (busy.address() as { port: number }).port;
  try {
    await page.getByLabel('Server port').fill(String(taken));
    await expect(page.getByTestId('port-status')).toHaveText(`Port ${taken} is already in use.`);
    await expect(page.getByRole('button', { name: 'Continue' })).toBeDisabled();
  } finally {
    await new Promise<void>((ok) => busy.close(() => ok()));
  }

  // Back to the listening port: Continue keeps the manager where it is and records the
  // port for step 5, so the next start (menu bar, `splash start`) uses it too.
  await page.getByLabel('Server port').fill(String(manager.port));
  await expect(page.getByTestId('port-status')).toHaveText('Current manager port');
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByText('Step 3 of 5', { exact: false })).toBeVisible();
  const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('splash-gui-wizard') ?? '{}'));
  expect(saved.pendingPort).toBe(manager.port);
});
