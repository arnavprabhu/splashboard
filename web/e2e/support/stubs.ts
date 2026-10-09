/**
 * `page.route` fallbacks for admin routes the backend has not finished (they answer 501).
 * Use only where a real route is missing, and name the stubbed route in the test title:
 *   test('download flow [stub: POST /downloads]', …)
 * `stubWhen501` forwards to the real manager first and substitutes the stub only when the
 * manager answers 501, so the test starts exercising the real route as soon as it lands.
 */

import type { Page, Route } from '@playwright/test';

export type StubBody = unknown | ((route: Route) => unknown | Promise<unknown>);

/** Always stub `method path` (glob on the URL path under /api/admin). */
export async function stubAdmin(page: Page, method: string, path: string, body: StubBody, status = 200): Promise<void> {
  await page.route(`**/api/admin${path}`, async (route) => {
    if (route.request().method() !== method) return route.fallback();
    const json = typeof body === 'function' ? await (body as (r: Route) => unknown)(route) : body;
    await route.fulfill({ status, json });
  });
}

/** Forward to the manager; replace the answer with `body` only when it is a 501 stub. */
export async function stubWhen501(page: Page, method: string, path: string, body: StubBody, status = 200): Promise<void> {
  await page.route(`**/api/admin${path}`, async (route) => {
    if (route.request().method() !== method) return route.fallback();
    const res = await route.fetch();
    if (res.status() !== 501) return route.fulfill({ response: res });
    const json = typeof body === 'function' ? await (body as (r: Route) => unknown)(route) : body;
    await route.fulfill({ status, json });
  });
}

/** An SSE body from (event, data) pairs, for stubbing streaming routes. */
export function sseBody(events: Array<[string, unknown]>): string {
  return events.map(([event, data], i) => `id: ${i + 1}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('');
}
