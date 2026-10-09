/**
 * Playground helpers (SPEC §10.6 Tools > Playground): the endpoint table and path parameters,
 * the "Copy as" snippets, the raw SSE parser and assembled message, and the 50-entry history.
 * The page itself is covered end to end (e2e/qa-batch2.spec.ts, the Playground rows).
 */
import { describe, expect, it } from 'vitest';
import {
  ENDPOINTS,
  defaultBody,
  endpointById,
  endpointFor,
  paramError,
  resolvePath,
  wantsStream,
} from '../src/routes/tools/endpoints';
import {
  DATA_URL_KEEP,
  HISTORY_KEY,
  HISTORY_MAX,
  SUMMARY_MAX,
  TRUNCATED,
  addEntry,
  loadHistory,
  removeEntry,
  saveHistory,
  summarizeResponse,
  truncateDataUrls,
  type HistoryEntry,
} from '../src/routes/tools/history';
import {
  RAW_NOTE,
  curlSnippet,
  jsFetchSnippet,
  pythonAnthropicSnippet,
  pythonOpenAiSnippet,
  shellQuote,
  snippet,
  snippetAvailable,
  toPython,
  typesafeSnippet,
  type SnippetInput,
} from '../src/routes/tools/snippets';
import {
  TimedSseParser,
  assemble,
  emptyAssembled,
  isTokenEvent,
  preview,
  pretty,
  responseIdOf,
  summarize,
  type RawEvent,
} from '../src/routes/tools/sse-timing';

const ORIGIN = 'http://127.0.0.1:8000';
const MODEL = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M';
const SECRET = 'splash-secret-value-123';

function input(id: string, body: unknown = null, extra: Partial<SnippetInput> = {}): SnippetInput {
  const endpoint = endpointById(id);
  return { origin: ORIGIN, endpoint, path: resolvePath(endpoint, ''), body, ...extra };
}

describe('endpoints', () => {
  it('every route is consistent: a body only on POST, a path parameter where the path has {id}', () => {
    for (const ep of ENDPOINTS) {
      expect(ep.body, ep.id).toBe(ep.method === 'POST');
      expect(ep.path.includes('{id}'), ep.id).toBe(ep.param !== undefined);
    }
  });

  it('finds the endpoint and its parameter for a concrete path', () => {
    expect(endpointFor('GET', '/v1/responses/resp_abc')).toEqual({ endpoint: endpointById('responses_get'), param: 'resp_abc' });
    expect(endpointFor('DELETE', '/v1/responses/resp_abc')?.endpoint.id).toBe('responses_delete');
    expect(endpointFor('GET', '/v1/models')).toEqual({ endpoint: endpointById('models'), param: '' });
    expect(endpointFor('POST', '/v1/models')).toBeNull();
    expect(endpointFor('GET', '/v1/responses/')).toBeNull();
    expect(endpointFor('GET', '/v1/models/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M')?.param).toBe(MODEL);
  });

  it('resolves {id}: response IDs are encoded, model IDs keep their slashes and colons', () => {
    expect(resolvePath(endpointById('responses_get'), ' resp_1 ')).toBe('/v1/responses/resp_1');
    expect(resolvePath(endpointById('model_get'), MODEL)).toBe(`/v1/models/${MODEL}`);
    expect(resolvePath(endpointById('model_get'), 'org/a b')).toBe('/v1/models/org/a%20b');
    expect(resolvePath(endpointById('chat'), '')).toBe('/v1/chat/completions');
  });

  it('reports a missing or malformed path parameter', () => {
    expect(paramError(endpointById('responses_get'), '')).toBe('missing');
    expect(paramError(endpointById('responses_get'), 'nope')).toBe('response_id');
    expect(paramError(endpointById('responses_get'), 'resp_ok_1')).toBeNull();
    expect(paramError(endpointById('model_get'), '  ')).toBe('missing');
    expect(paramError(endpointById('chat'), '')).toBeNull();
  });

  it('streams only when the body says stream: true', () => {
    expect(wantsStream({ stream: true })).toBe(true);
    expect(wantsStream({ stream: 'true' })).toBe(false);
    expect(wantsStream(null)).toBe(false);
  });

  it('pre-fills a chat body with the active model', () => {
    const body = JSON.parse(defaultBody('chat', MODEL)) as { model?: string; messages?: unknown };
    expect(body.model).toBe(MODEL);
    expect(Array.isArray(body.messages)).toBe(true);
  });
});

describe('snippets (SPEC §10.6, D-09-7)', () => {
  it('shell-quotes single quotes for POSIX shells', () => {
    expect(shellQuote("it's")).toBe(`'it'\\''s'`);
  });

  it('converts JSON to Python literals without touching string contents', () => {
    expect(toPython({ a: true, b: null, c: 'true', d: false })).toBe('{\n    "a": True,\n    "b": None,\n    "c": "true",\n    "d": False\n}');
    expect(toPython(undefined)).toBe('None');
    // A base indent moves every line after the first, so the block nests inside a Python call.
    expect(toPython({ x: [1] }, 4, 8)).toBe('{\n            "x": [\n                1\n            ]\n        }');
  });

  it('builds a runnable curl command for a streamed chat request', () => {
    const body = { model: MODEL, stream: true, messages: [{ role: 'user', content: "it's" }] };
    const out = curlSnippet(input('chat', body));
    expect(out).toContain(`curl ${ORIGIN}/v1/chat/completions`);
    expect(out).toContain(`-H 'Content-Type: application/json'`);
    expect(out).toContain(' -N ');
    expect(out).toContain(`-d '{"model":"${MODEL}","stream":true,"messages":[{"role":"user","content":"it'\\''s"}]}'`);
    expect(out).not.toContain('Authorization');
  });

  it('uses -X for a route without a body, and a note when sent raw', () => {
    const out = curlSnippet(input('models', null, { raw: true }));
    expect(out.split('\n')[0]).toBe(`# ${RAW_NOTE}`);
    expect(out).toContain(`curl -X GET ${ORIGIN}/v1/models`);
  });

  it('references SPLASH_API_KEY and never the value when a key is required (D-09-7)', () => {
    const body = { model: MODEL, messages: [] };
    const outputs = [
      curlSnippet(input('chat', body, { auth: true })),
      pythonOpenAiSnippet(input('chat', body, { auth: true })),
      pythonAnthropicSnippet(input('messages', body, { auth: true })) ?? '',
      jsFetchSnippet(input('chat', body, { auth: true })),
      pythonOpenAiSnippet(input('tokenize', body, { auth: true })),
    ];
    for (const out of outputs) {
      expect(out).toMatch(/SPLASH_API_KEY/);
      expect(out).not.toContain(SECRET);
    }
    expect(curlSnippet(input('chat', body, { auth: true }))).toContain('Authorization: Bearer $SPLASH_API_KEY');
    expect(pythonOpenAiSnippet(input('chat', body, { auth: true }))).toContain('api_key=os.environ["SPLASH_API_KEY"]');
    expect(jsFetchSnippet(input('chat', body, { auth: true }))).toContain('process.env.SPLASH_API_KEY');
    expect(pythonOpenAiSnippet(input('tokenize', body, { auth: true }))).toContain("os.environ['SPLASH_API_KEY']");
  });

  it('uses a placeholder key without auth, and never embeds one', () => {
    expect(pythonOpenAiSnippet(input('chat', { model: MODEL }))).toContain('api_key="local"');
    expect(pythonOpenAiSnippet(input('chat', { model: MODEL }))).not.toContain('import os');
  });

  it('targets the OpenAI SDK for /v1 routes and httpx for the rest', () => {
    const chat = pythonOpenAiSnippet(input('chat', { model: MODEL, stream: true }));
    expect(chat).toContain('from openai import OpenAI');
    expect(chat).toContain(`base_url="${ORIGIN}/v1"`);
    expect(chat).toContain('stream = client.chat.completions.create(**body)');
    expect(chat).toContain('for event in stream:');
    expect(pythonOpenAiSnippet(input('responses_get', null, { path: '/v1/responses/resp_x' }))).toContain('client.responses.retrieve("resp_x")');
    expect(pythonOpenAiSnippet(input('tokenize', { text: 'hi' }))).toContain(`httpx.post("${ORIGIN}/tokenize", json=body, timeout=None)`);
    expect(pythonOpenAiSnippet(input('metrics'))).toContain('print(response.text)');
  });

  it('sends a System One request through the typesafe-sdk snippet', () => {
    const body = {
      model: MODEL,
      state: 'hello',
      questions: {
        intent: { type: 'choice', criteria: { billing: 'money', other: null } },
        mood: { type: 'noul', instructions: 'Is it happy?' },
        grade: { type: 'score', criteria: ['low', 'high'] },
      },
    };
    const out = pythonOpenAiSnippet(input('systemone', body));
    expect(out.split('\n')[0]).toBe('from typesafe_sdk import Choice, Noul, Score, TypeSafeClient');
    expect(out).toContain('"intent": Choice(criteria={');
    expect(out).toContain('"mood": Noul(instructions="Is it happy?"),');
    expect(out).toContain('"grade": Score(criteria=');
    expect(out).toContain(`base_url="${ORIGIN}"`);
  });

  it('builds the typesafe snippet with the model and the state', () => {
    const out = typesafeSnippet({ origin: ORIGIN, auth: false, body: { model: MODEL, state: { a: 1 }, questions: {} } });
    expect(out).toContain(`model="${MODEL}"`);
    expect(out).toContain('state={\n');
    expect(out).toContain('api_key="local"');
  });

  it('gives the Anthropic snippet only for the messages routes', () => {
    expect(snippetAvailable('python_anthropic', endpointById('messages'))).toBe(true);
    expect(snippetAvailable('python_anthropic', endpointById('count_tokens'))).toBe(true);
    expect(snippetAvailable('python_anthropic', endpointById('chat'))).toBe(false);
    expect(pythonAnthropicSnippet(input('chat', { model: MODEL }))).toBeNull();
    const out = pythonAnthropicSnippet(input('messages', { model: MODEL, stream: true }));
    expect(out).toContain(`anthropic.Anthropic(base_url="${ORIGIN}", api_key="local")`);
    expect(out).toContain('for event in stream:');
    expect(pythonAnthropicSnippet(input('count_tokens', { model: MODEL }))).toContain('client.messages.count_tokens(**body)');
  });

  it('writes a fetch snippet with the stream reader and the key reference', () => {
    const out = jsFetchSnippet(input('chat', { model: MODEL, stream: true }, { auth: true }));
    expect(out).toContain(`await fetch('${ORIGIN}/v1/chat/completions', {`);
    expect(out).toContain("method: 'POST'");
    expect(out).toContain('Authorization: `Bearer ${process.env.SPLASH_API_KEY}`');
    expect(out).toContain('reader.read()');
    expect(jsFetchSnippet(input('metrics'))).toContain('console.log(await res.text());');
  });

  it('dispatches by kind and marks raw requests in each language', () => {
    const chat = input('chat', { model: MODEL }, { raw: true });
    expect(snippet('curl', chat)).toContain(RAW_NOTE);
    expect(snippet('python_openai', chat)).toContain(`# ${RAW_NOTE}`);
    expect(snippet('python_anthropic', chat)).toBeNull();
    expect(snippet('js_fetch', chat)).toContain(`// ${RAW_NOTE}`);
  });
});

describe('raw SSE parser and timing (SPEC §10.6)', () => {
  it('reads one event across chunk boundaries', () => {
    const p = new TimedSseParser();
    expect(p.push('data: {"a"')).toEqual([]);
    expect(p.push(':1}\n\n')).toEqual([{ kind: 'event', name: 'data', data: '{"a":1}' }]);
  });

  it('keeps the event name and joins multi-line data', () => {
    const p = new TimedSseParser();
    const out = p.push('event: response.output_text.delta\ndata: one\ndata: two\n\n');
    expect(out).toEqual([{ kind: 'event', name: 'response.output_text.delta', data: 'one\ntwo' }]);
  });

  it('handles CRLF line endings', () => {
    const p = new TimedSseParser();
    expect(p.push('event: content_block_delta\r\ndata: {"x":1}\r\n\r\n')).toEqual([{ kind: 'event', name: 'content_block_delta', data: '{"x":1}' }]);
  });

  it('keeps keepalive comments, which the raw view shows', () => {
    const p = new TimedSseParser();
    expect(p.push(': splash-keepalive\n\n')).toEqual([{ kind: 'comment', name: 'comment', data: 'splash-keepalive' }]);
  });

  it('flushes a last event that has no blank line after it', () => {
    const p = new TimedSseParser();
    expect(p.push('data: [DONE]')).toEqual([]);
    expect(p.end()).toEqual([{ kind: 'event', name: 'data', data: '[DONE]' }]);
  });

  it('finds the first token and summarises the stream', () => {
    const rows: RawEvent[] = [
      { index: 1, t: 0, kind: 'open', name: 'open', data: '' },
      { index: 2, t: 10, kind: 'event', name: 'data', data: '{"choices":[{"delta":{"role":"assistant"}}]}' },
      { index: 3, t: 14, kind: 'comment', name: 'comment', data: 'splash-keepalive' },
      { index: 4, t: 20, kind: 'event', name: 'data', data: '{"choices":[{"delta":{"content":"Hi"}}]}' },
      { index: 5, t: 30, kind: 'event', name: 'data', data: '[DONE]' },
    ];
    expect(isTokenEvent('chat', rows[1]!)).toBe(false);
    expect(isTokenEvent('chat', rows[3]!)).toBe(true);
    expect(isTokenEvent('chat', rows[2]!)).toBe(false);
    expect(summarize('chat', rows, 35)).toEqual({ events: 3, firstData: 10, firstToken: 20, done: 35 });
    expect(summarize('chat', rows.slice(0, 1), null)).toEqual({ events: 0, firstData: null, firstToken: null, done: null });
  });

  it('recognises the token event of each stream shape', () => {
    expect(isTokenEvent('responses', { kind: 'event', name: 'response.output_text.delta', data: '{}' })).toBe(true);
    expect(isTokenEvent('responses', { kind: 'event', name: 'response.created', data: '{}' })).toBe(false);
    expect(isTokenEvent('messages', { kind: 'event', name: 'content_block_delta', data: '{}' })).toBe(true);
    expect(isTokenEvent('completions', { kind: 'event', name: 'data', data: '{"choices":[{"text":"x"}]}' })).toBe(true);
    expect(isTokenEvent('completions', { kind: 'event', name: 'data', data: '{"choices":[{"text":""}]}' })).toBe(false);
  });

  it('previews one flat line and pretty-prints JSON, leaving other text alone', () => {
    expect(preview('a \n  b')).toBe('a b');
    expect(preview('x'.repeat(200), 10)).toBe(`${'x'.repeat(9)}…`);
    expect(pretty('{"a":[1]}')).toBe('{\n  "a": [\n    1\n  ]\n}');
    expect(pretty('not json')).toBe('not json');
  });
});

describe('assembled message (PARSED view)', () => {
  it('joins chat deltas, reasoning, tool calls, finish reason and usage', () => {
    const shape = 'chat' as const;
    let acc = emptyAssembled();
    const events = [
      '{"choices":[{"delta":{"content":"Hel"}}]}',
      '{"choices":[{"delta":{"content":"lo","reasoning_content":"think"}}]}',
      '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"get_","arguments":"{\\"a\\":"}}]}}]}',
      '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"weather","arguments":"1}"}}]},"finish_reason":"tool_calls"}],"usage":{"total_tokens":9}}',
      '[DONE]',
    ];
    for (const data of events) acc = assemble(shape, acc, 'data', data);
    expect(acc.text).toBe('Hello');
    expect(acc.reasoning).toBe('think');
    expect(acc.toolCalls).toEqual([{ id: 'c1', name: 'get_weather', arguments: '{"a":1}' }]);
    expect(acc.finish).toBe('tool_calls');
    expect(acc.usage).toEqual({ total_tokens: 9 });
  });

  it('assembles a Responses stream and keeps the response ID for GET and DELETE', () => {
    let acc = emptyAssembled();
    acc = assemble('responses', acc, 'response.output_text.delta', '{"delta":"Hi"}');
    acc = assemble('responses', acc, 'response.completed', '{"response":{"id":"resp_42","status":"completed","usage":{"output_tokens":2}}}');
    expect(acc.text).toBe('Hi');
    expect(acc.finish).toBe('completed');
    expect(responseIdOf(acc.response)).toBe('resp_42');
    expect(responseIdOf({ id: 'chatcmpl-1' })).toBeNull();
  });

  it('assembles a Messages stream with tool use and the stop reason', () => {
    let acc = emptyAssembled();
    acc = assemble('messages', acc, 'message_start', '{"message":{"usage":{"input_tokens":5}}}');
    acc = assemble('messages', acc, 'content_block_start', '{"content_block":{"type":"tool_use","id":"t1","name":"lookup"}}');
    acc = assemble('messages', acc, 'content_block_delta', '{"delta":{"partial_json":"{\\"q\\":2}"}}');
    acc = assemble('messages', acc, 'content_block_delta', '{"delta":{"text":"ok"}}');
    acc = assemble('messages', acc, 'message_delta', '{"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}');
    expect(acc.toolCalls).toEqual([{ id: 't1', name: 'lookup', arguments: '{"q":2}' }]);
    expect(acc.text).toBe('ok');
    expect(acc.finish).toBe('end_turn');
    expect(acc.usage).toEqual({ input_tokens: 5, output_tokens: 3 });
  });

  it('keeps an error event as the error, and ignores the [DONE] sentinel', () => {
    let acc = emptyAssembled();
    acc = assemble('chat', acc, 'data', '[DONE]');
    expect(acc.error).toBeNull();
    acc = assemble('chat', acc, 'data', '{"error":{"message":"boom"}}');
    expect(acc.error).toEqual({ error: { message: 'boom' } });
  });
});

describe('history (SPEC §10.6: last 50 requests in localStorage)', () => {
  function fakeStorage(seed?: string, failWrites = false): Storage {
    const data = new Map<string, string>(seed ? [[HISTORY_KEY, seed]] : []);
    return {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => {
        if (failWrites) throw new Error('quota');
        data.set(k, v);
      },
      removeItem: (k: string) => data.delete(k),
      clear: () => data.clear(),
      key: () => null,
      get length() {
        return data.size;
      },
    } as Storage;
  }

  const base = { method: 'POST', path: '/v1/chat/completions', mode: 'profiles' as const, model: MODEL, status: 200, duration_ms: 5 };

  it('cuts data URLs and base64 fields to 64 characters and says so', () => {
    const url = `data:image/png;base64,${'A'.repeat(300)}`;
    const cut = truncateDataUrls(JSON.stringify({ image_url: url }));
    expect(cut.truncated).toBe(true);
    expect(cut.text).toContain(url.slice(0, DATA_URL_KEEP) + TRUNCATED);
    expect(cut.text).not.toContain('A'.repeat(100));

    const b64 = `{"type":"base64","data":"${'B'.repeat(100)}"}`;
    const field = truncateDataUrls(b64);
    expect(field.truncated).toBe(true);
    expect(field.text).toBe(`{"type":"base64","data":"${'B'.repeat(DATA_URL_KEEP)}${TRUNCATED}"}`);

    expect(truncateDataUrls('{"text":"short"}')).toEqual({ text: '{"text":"short"}', truncated: false });
  });

  it('caps the response summary and flattens its whitespace', () => {
    expect(summarizeResponse(`a\n\n  b ${'x'.repeat(300)}`).length).toBe(SUMMARY_MAX);
    expect(summarizeResponse('  one\ttwo\nthree  ')).toBe('one two three');
  });

  it('keeps the newest entry first and never more than 50', () => {
    let list: HistoryEntry[] = [];
    for (let i = 0; i < HISTORY_MAX + 5; i++) {
      list = addEntry(list, { ...base, ts: 1000 + i, path: `/v1/chat/completions?n=${i}`, body: null, response_summary: `reply ${i}` });
    }
    expect(list).toHaveLength(HISTORY_MAX);
    expect(list[0]!.response_summary).toBe(`reply ${HISTORY_MAX + 4}`);
    expect(list.at(-1)!.response_summary).toBe('reply 5');
    expect(new Set(list.map((e) => e.id)).size).toBe(HISTORY_MAX);
  });

  it('stores the body with data URLs cut, and flags the entry', () => {
    const body = JSON.stringify({ image: `data:image/png;base64,${'C'.repeat(200)}` });
    const [entry] = addEntry([], { ...base, ts: 1, body, response_summary: 'ok' });
    expect(entry!.truncated).toBe(true);
    expect(entry!.body).toContain(TRUNCATED);
    const [plain] = addEntry([], { ...base, ts: 2, body: '{"a":1}', response_summary: 'ok' });
    expect(plain!.truncated).toBeUndefined();
    expect(plain!.body).toBe('{"a":1}');
  });

  it('removes one entry by ID', () => {
    const list = addEntry(addEntry([], { ...base, ts: 1, body: null, response_summary: 'a' }), { ...base, ts: 2, body: null, response_summary: 'b' });
    const [keep] = list;
    const left = removeEntry(list, list[1]!.id);
    expect(left.map((e) => e.id)).toEqual([keep!.id]);
  });

  it('saves at most 50, loads only well-formed entries, and survives bad storage', () => {
    const many = Array.from({ length: HISTORY_MAX + 10 }, (_, i) => ({ id: `i${i}`, ts: i, method: 'GET', path: '/status' }));
    const store = fakeStorage();
    saveHistory(many as HistoryEntry[], store);
    expect(JSON.parse(store.getItem(HISTORY_KEY)!)).toHaveLength(HISTORY_MAX);
    expect(loadHistory(store)).toHaveLength(HISTORY_MAX);

    const mixed = fakeStorage(JSON.stringify([{ id: 'ok', ts: 1, method: 'GET', path: '/x' }, { nope: true }, 'text']));
    expect(loadHistory(mixed).map((e) => e.id)).toEqual(['ok']);
    expect(loadHistory(fakeStorage('{not json'))).toEqual([]);
    expect(loadHistory(fakeStorage('{"a":1}'))).toEqual([]);
    expect(loadHistory(null)).toEqual([]);

    expect(() => saveHistory(many as HistoryEntry[], fakeStorage(undefined, true))).not.toThrow();
    expect(() => saveHistory([], null)).not.toThrow();
  });
});
