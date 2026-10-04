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
    cancelled: 3,
    duration_ms: 3_120_000,
    decode_tps_avg: 74.2,
    top_clients: [
      { client: 'claude-code/1.2.3', requests: 812, prompt_tokens: 2_884_112, completion_tokens: 402_118, total_tokens: 3_286_230, last_seen_at: '2026-10-03T11:58:00+00:00' },
      { client: 'chat.openai.com', requests: 402, prompt_tokens: 1_204_338, completion_tokens: 480_221, total_tokens: 1_684_559, last_seen_at: '2026-10-02T09:00:00+00:00' },
      { client: 'curl/8.7.1', requests: 70, prompt_tokens: 94_541, completion_tokens: 36_103, total_tokens: 130_644, last_seen_at: null },
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
      cancelled: 0,
      prompt_tokens: 400_000 + day * 20_000,
      cached_tokens: 180_000 + day * 9_000,
      completion_tokens: 90_000 + day * 4_000,
    })),
    heatmap: HEATMAP,
    heatmap_tokens: HEATMAP.map((row) => row.map((n) => n * 3100)),
  },
  facets: {
    models: [MODEL, 'unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M'],
    endpoints: ['/v1/chat/completions', '/v1/messages'],
    clients: ['claude-code/1.2.3', 'chat.openai.com', 'curl/8.7.1'],
    profiles: ['thinking'],
    statuses: ['2xx', '4xx', '5xx', 'cancelled'],
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
    next_cursor: '4950',
    total: 1284,
    offset: 0,
    limit: 50,
  },
};

/** Integrations as `GET /integrations` returns them (docs/api.md §12.1). */
export const INTEGRATIONS = {
  cli: [
    { name: 'claude', label: 'Claude Code', installed: true, path: '/usr/local/bin/claude', version: '2.1.4', install_url: 'https://code.claude.com/docs/en/overview', command: 'splash launch claude', changes: { env: { ANTHROPIC_BASE_URL: 'http://127.0.0.1:8000', ANTHROPIC_AUTH_TOKEN: 'local' }, args: ['--disallowedTools', 'WebSearch'], files: [], notes: [] }, entries: [], last_launched_at: '2026-10-04T09:12:00+00:00' },
    { name: 'codex', label: 'Codex CLI', installed: true, path: '/usr/local/bin/codex', version: '0.58.0', install_url: 'https://developers.openai.com/codex/cli/', command: 'splash launch codex', changes: { env: { SPLASH_API_KEY: 'local' }, args: ['-c', 'model_provider=splash'], files: [], notes: [] }, entries: [], last_launched_at: null },
    { name: 'opencode', label: 'OpenCode', installed: false, path: null, version: null, install_url: 'https://opencode.ai/docs/', command: 'splash launch opencode', changes: { env: {}, args: [], files: [], notes: [] }, entries: [], last_launched_at: null },
    { name: 'hermes', label: 'Hermes', installed: true, path: '/usr/local/bin/hermes', version: '2026.9.12', install_url: 'https://hermes-agent.nousresearch.com/docs/getting-started/installation/', command: 'splash launch hermes', changes: { env: {}, args: [], files: ['~/.hermes/profiles/splash/config.yaml'], notes: [] }, entries: ['splash'], last_launched_at: null },
    { name: 'pi', label: 'Pi', installed: true, path: '/usr/local/bin/pi', version: '0.9.3', install_url: 'https://pi.dev/', command: 'splash launch pi', changes: { env: {}, args: ['--provider', 'splash'], files: ['~/.pi/agent/models.json'], notes: [] }, entries: ['splash-8000'], last_launched_at: null },
  ],
  desktop: [
    { name: 'claude-desktop', label: 'Claude Desktop', detected: true, app_path: '/Applications/Claude.app', version: '1.4.0', untested_version: false, running: true, state: 'not_connected', connected_at: null, warning: '' },
    { name: 'codex-app', label: 'Codex app', detected: true, app_path: '/Applications/Codex.app', version: '1.2026.0925', untested_version: true, running: false, state: 'not_connected', connected_at: null, warning: '' },
  ],
  unclean_shutdown: false,
};

/** One saved conversation (docs/api.md §8). */
export const CHAT = {
  id: 'c1',
  title: 'PDF action items',
  created_at: '2026-10-04T09:00:00+00:00',
  updated_at: '2026-10-04T09:05:00+00:00',
  model: MODEL,
  profile: 'default',
  system: '',
  sampling: {},
  tools: [],
  tool_choice: 'auto',
  response_format: null,
  messages: [
    { id: 'm1', parent: null, role: 'user', content: 'Summarise this.', created_at: '2026-10-04T09:00:00+00:00', attachments: [] },
    {
      id: 'm2',
      parent: 'm1',
      role: 'assistant',
      content: 'Here are the action items.',
      created_at: '2026-10-04T09:00:05+00:00',
      meta: { usage: { prompt_tokens: 12288, completion_tokens: 431, prompt_tokens_details: { cached_tokens: 8192 } }, timings: { predicted_per_second: 74.2 }, finish_reason: 'stop', ttft_ms: 310, model: MODEL, profile: 'default' },
    },
  ],
  active_leaf: 'm2',
};

export const V1_MODELS = { object: 'list', data: [{ id: MODEL, loaded: true, max_model_len: 262144, vision: true }, { id: `${MODEL}:no-think`, root: MODEL, profile: 'no-think', loaded: true }] };

export interface MockOptions {
  alerts?: unknown[];
  auth?: { admin_requires_key: boolean; authenticated: boolean; method: string | null };
  /** Serve populated `/usage/*` payloads; omit for the 501 default. */
  usage?: typeof USAGE;
  /** Extra per-test answers, tried before the defaults; return undefined to fall through. */
  extra?: (method: string, path: string, url: URL, body: unknown) => { status?: number; json?: unknown } | undefined;
  engine?: Record<string, unknown>;
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
    const url = new URL(req.url());
    let body: unknown = null;
    try {
      body = req.postDataJSON();
    } catch {
      body = null;
    }
    const hit = opts.extra?.(req.method(), path, url, body);
    if (hit) return route.fulfill({ status: hit.status ?? 200, json: hit.json ?? {} });
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
    if (path === '/engine') return route.fulfill({ json: opts.engine ?? ENGINE });
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
      if (!usage) return route.fulfill({ status: 501, json: NOT_IMPLEMENTED });
      const page = Number(url.searchParams.get('page') ?? 1);
      const shift = (page - 1) * 50;
      return route.fulfill({ json: { ...usage.rows, offset: shift, rows: usage.rows.rows.map((r) => ({ ...r, id: r.id - shift })) } });
    }
    if (path === '/usage/facets') {
      return usage ? route.fulfill({ json: usage.facets }) : route.fulfill({ status: 501, json: NOT_IMPLEMENTED });
    }
    return route.fulfill({ status: 501, json: NOT_IMPLEMENTED });
  });
  return calls;
}
