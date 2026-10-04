import type { Page } from '@playwright/test';

/** Manager-shaped answers (manager/splash_gui/schemas.py) for a stopped engine with auth off. */
export const ENGINE = {
  state: 'stopped',
  model: null,
  since: '2026-10-03T12:00:00+00:00',
  restart: { auto_restart: true },
  engine: { found: true, version: '1.2.0', support: 'supported' },
};

export const SETTINGS = {
  settings: { version: 1, global: { ui: { theme: 'light' }, wizard: { completed: true } }, models: {} },
};

const MODEL = 'mlx-community/Qwen3.6-35B-A3B-4bit';

/** 7 × 24 request counts, Monday first, as `/usage/timeseries?view=heatmap` returns them. */
const HEATMAP = Array.from({ length: 7 }, (_, day) =>
  Array.from({ length: 24 }, (_, hour) => ((day * 7 + hour * 3) % 23) + (day % 3)),
);

/**
 * A populated Usage History payload. The phone-width check must see real content:
 * an empty heatmap and an empty request-log table never exercise the widest boxes.
 */
export const USAGE = {
  summary: {
    scope: 'all',
    since: '2026-10-01T00:00:00+00:00',
    requests: 1284,
    completed: 1270,
    failed: 14,
    prompt_tokens: 4_182_991,
    cached_tokens: 2_004_113,
    completion_tokens: 918_442,
    total_tokens: 5_101_433,
    cache_efficiency: 0.479,
    ttft_p50_ms: 184,
    ttft_p95_ms: 612,
    by_model: [
      {
        model: MODEL,
        requests: 1204,
        prompt_tokens: 4_010_882,
        cached_tokens: 1_990_442,
        completion_tokens: 861_233,
        last_used_at: '2026-10-03T11:58:00+00:00',
      },
    ],
    top_clients: [
      { client: 'claude-code/1.2.3', requests: 812, prompt_tokens: 2_884_112, completion_tokens: 402_118 },
      { client: 'chat.openai.com', requests: 402, prompt_tokens: 1_204_338, completion_tokens: 480_221 },
      { client: 'curl/8.7.1', requests: 70, prompt_tokens: 94_541, completion_tokens: 36_103 },
    ],
  },
  timeseries: {
    bucket: 'day',
    group_by: 'none',
    start: '2026-09-27T00:00:00+00:00',
    end: '2026-10-03T00:00:00+00:00',
    points: Array.from({ length: 7 }, (_, day) => ({
      t: `2026-09-${String(27 + day).padStart(2, '0')}T00:00:00+00:00`,
      group: null,
      requests: 120 + day * 9,
      errors: day,
      prompt_tokens: 400_000 + day * 20_000,
      cached_tokens: 180_000 + day * 9_000,
      completion_tokens: 90_000 + day * 4_000,
    })),
    heatmap: HEATMAP,
  },
  rows: {
    rows: Array.from({ length: 50 }, (_, i) => ({
      id: 5000 - i,
      ts: new Date(Date.UTC(2026, 9, 3, 11, 58 - i)).toISOString(),
      model: MODEL,
      profile: i % 4 === 0 ? 'thinking' : null,
      endpoint: '/v1/chat/completions',
      client: 'claude-code/1.2.3',
      stream: true,
      status: i % 23 === 0 ? 503 : 200,
      error_code: i % 23 === 0 ? 'engine_unavailable' : null,
      prompt_tokens: 3_400 + i,
      cached_tokens: 2_100 + i,
      completion_tokens: 288 + i,
      ttft_ms: 180 + i,
      prompt_ms: 240 + i,
      predicted_ms: 2_100 + i * 3,
      duration_ms: 2_400 + i * 3,
      injected: { temperature: 0.7, top_p: 0.8, reasoning_effort: 'none' },
    })),
    next_cursor: '4800',
  },
};

export interface MockOptions {
  alerts?: unknown[];
  auth?: { admin_requires_key: boolean; authenticated: boolean; method: string | null };
  /** Serve populated `/usage/*` payloads; omit for the 501 default. */
  usage?: typeof USAGE;
}

const NOT_IMPLEMENTED = {
  error: { message: 'not in smoke tests', type: 'not_implemented', code: 'not_implemented' },
};

/** Stubs the manager API and returns the list of calls it saw. */
export async function mockManager(page: Page, opts: MockOptions = {}): Promise<string[]> {
  const calls: string[] = [];
  const auth = { ...(opts.auth ?? { admin_requires_key: false, authenticated: true, method: 'open' }) };
  const usage = opts.usage;
  await page.route('**/health', (route) => route.fulfill({ json: { status: 'ok', version: '0.1.0' } }));
  await page.route('**/api/admin/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace('/api/admin', '');
    calls.push(`${req.method()} ${path}`);
    if (path === '/auth/state') return route.fulfill({ json: auth });
    if (path === '/auth/login') {
      const ok = (req.postDataJSON() as { key?: string }).key === 'secret';
      if (!ok)
        return route.fulfill({
          status: 401,
          json: { error: { message: 'invalid key', type: 'authentication_error', code: 'invalid_key' } },
        });
      auth.authenticated = true;
      auth.method = 'session';
      return route.fulfill({ json: auth });
    }
    if (auth.admin_requires_key && !auth.authenticated) {
      return route.fulfill({
        status: 401,
        json: { error: { message: 'Sign in required', type: 'authentication_error', code: 'unauthorized' } },
      });
    }
    if (path === '/engine') return route.fulfill({ json: ENGINE });
    if (path === '/settings') return route.fulfill({ json: SETTINGS });
    if (path === '/alerts') return route.fulfill({ json: { alerts: opts.alerts ?? [] } });
    if (path.endsWith('/dismiss') || path === '/engine/restart') return route.fulfill({ json: { ok: true } });
    if (path === '/usage/summary') {
      return usage ? route.fulfill({ json: usage.summary }) : route.fulfill({ status: 501, json: NOT_IMPLEMENTED });
    }
    if (path === '/usage/timeseries') {
      return usage ? route.fulfill({ json: usage.timeseries }) : route.fulfill({ status: 501, json: NOT_IMPLEMENTED });
    }
    if (path === '/usage/requests') {
      return usage ? route.fulfill({ json: usage.rows }) : route.fulfill({ status: 501, json: NOT_IMPLEMENTED });
    }
    return route.fulfill({ status: 501, json: NOT_IMPLEMENTED });
  });
  return calls;
}
