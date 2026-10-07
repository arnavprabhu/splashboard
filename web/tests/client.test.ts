import { describe, expect, it, vi } from 'vitest';
import { api, ApiError, buildUrl, parseErrorBody, request, setUnauthorizedHandler } from '../src/api/client';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('parseErrorBody', () => {
  it('reads the OpenAI-shaped Splash error', () => {
    expect(
      parseErrorBody({ error: { message: 'Queue full', type: 'server_error', code: 'queue_full' } }, 503),
    ).toEqual({ message: 'Queue full', type: 'server_error', code: 'queue_full' });
  });
  it('reads the Anthropic shape', () => {
    expect(parseErrorBody({ type: 'error', error: { type: 'invalid_request_error', message: 'bad' } }, 400)).toEqual({
      message: 'bad',
      type: 'invalid_request_error',
      code: null,
    });
  });
  it('maps FastAPI detail strings and validation lists', () => {
    expect(parseErrorBody({ detail: 'Not found' }, 404)).toEqual({ message: 'Not found', type: 'not_found_error', code: null });
    expect(
      parseErrorBody({ detail: [{ loc: ['body', 'serve', 'port'], msg: 'too big' }, { loc: ['query', 'q'], msg: 'required' }] }, 422),
    ).toEqual({ message: 'serve.port: too big; query.q: required', type: 'invalid_request_error', code: 'validation_error' });
    expect(parseErrorBody({ detail: { message: 'x', type: 't', code: 'c' } })).toEqual({ message: 'x', type: 't', code: 'c' });
  });
  it('keeps the manager issues and details extensions', () => {
    const body = parseErrorBody(
      {
        error: {
          message: 'Settings are invalid',
          type: 'invalid_request_error',
          code: 'invalid_settings',
          issues: [{ path: ['global', 'cache', 'cache_dir'], key: 'cache.cache_dir', message: '--cache-dir requires --persistent-cache' }],
          details: { field: 'x' },
        },
      },
      422,
    );
    expect(body?.issues).toEqual([
      { path: ['global', 'cache', 'cache_dir'], key: 'cache.cache_dir', message: '--cache-dir requires --persistent-cache' },
    ]);
    expect(body?.details).toEqual({ field: 'x' });
    const err = new ApiError(422, body!);
    expect(err.issues).toHaveLength(1);
    expect(new ApiError(500, { message: 'x', type: 'server_error', code: null }).issues).toEqual([]);
  });

  it('returns null for unknown shapes', () => {
    expect(parseErrorBody('oops')).toBeNull();
    expect(parseErrorBody({ ok: true })).toBeNull();
  });
});

describe('buildUrl', () => {
  it('drops null/undefined and appends correctly', () => {
    expect(buildUrl('/a', { q: 'x y', n: 2, skip: undefined, none: null, b: false })).toBe('/a?q=x+y&n=2&b=false');
    expect(buildUrl('/a?x=1', { y: 2 })).toBe('/a?x=1&y=2');
    expect(buildUrl('/a', {})).toBe('/a');
  });
});

describe('request', () => {
  it('sends JSON and parses JSON', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(200, { restart_required: true }));
    const out = await api.put<{ restart_required: boolean }>('/settings', { a: 1 });
    expect(out.restart_required).toBe(true);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('/api/admin/settings');
    expect(init?.method).toBe('PUT');
    expect(init?.body).toBe('{"a":1}');
    expect((init?.headers as Record<string, string>)['Content-Type']).toBe('application/json');
  });

  it('returns undefined for 204', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api.del('/logs')).resolves.toBeUndefined();
  });

  it('throws ApiError with Splash fields', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(503, { error: { message: 'Queue full (32)', type: 'server_error', code: 'queue_full' } }),
    );
    const err = await request('/v1/chat/completions', { body: {} }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 503, code: 'queue_full', type: 'server_error', message: 'Queue full (32)' });
  });

  it('uses the status text for non-JSON errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 502, statusText: 'Bad Gateway' }));
    await expect(request('/x')).rejects.toMatchObject({ status: 502, message: '502 Bad Gateway', type: 'server_error' });
  });

  it('reports network failures as ApiError status 0', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(request('/x')).rejects.toMatchObject({ status: 0, code: 'network' });
  });

  it('api.read sends a read-only POST with the query (D58: /doctor, /system, /inspect, …)', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse(200, { ok: true }));
    await expect(api.read('/inspect', { id: 'a/b:Q4', refresh: true })).resolves.toEqual({ ok: true });
    const [url, init] = spy.mock.calls[0]!;
    expect(url).toBe('/api/admin/inspect?id=a%2Fb%3AQ4&refresh=true');
    expect(init?.method).toBe('POST');
    expect(init?.body).toBe('{}');
  });

  it('only auth_required (or an uncoded 401) signs the user in again', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const body = (code: string) => ({ error: { message: 'x', type: 'authentication_error', code } });
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(401, body('invalid_code')))
      .mockResolvedValueOnce(jsonResponse(401, body('invalid_key')))
      .mockResolvedValueOnce(jsonResponse(401, body('auth_required')));
    await expect(api.post('/auth/exchange', { code: 'c' })).rejects.toMatchObject({ code: 'invalid_code' });
    await expect(api.post('/auth/login', { key: 'k' })).rejects.toMatchObject({ code: 'invalid_key' });
    await expect(api.put('/settings', {})).rejects.toMatchObject({ code: 'auth_required' });
    expect(handler).toHaveBeenCalledTimes(1);
    setUnauthorizedHandler(null);
  });

  it('calls the unauthorized handler for admin 401s only', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse(401, { detail: 'login required' }));
    await expect(api.get('/engine')).rejects.toMatchObject({ status: 401 });
    await expect(request('/v1/models')).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
    setUnauthorizedHandler(null);
  });
});
