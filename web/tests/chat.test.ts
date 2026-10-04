import { describe, expect, it } from 'vitest';
import { ApiError } from '../src/api/client';
import {
  attachmentError,
  autoTitle,
  checkResult,
  checkTools,
  classifyError,
  countPdfPages,
  emptySampling,
  groupChats,
  hasSamplingErrors,
  listTime,
  modelRows,
  numError,
  outputBody,
  outputError,
  outputForm,
  requestModel,
  samplingBody,
  samplingErrors,
  samplingForm,
  schemaError,
  shortModel,
  splitModel,
  stopError,
  toolChoiceBody,
  toolChoiceError,
} from '../src/routes/chat/logic';
import type { ChatSummary } from '../src/routes/chat/types';

const NOW = new Date(2026, 9, 4, 15, 0, 0);

describe('sampling panel (docs/ui/07 §9.1)', () => {
  it('sends nothing at model defaults', () => {
    expect(samplingBody(emptySampling(), { allowIgnoreEos: true })).toEqual({});
  });

  it('sends only typed fields, as numbers', () => {
    const f = emptySampling();
    f.num.temperature = '0.3';
    f.num.max_completion_tokens = '512';
    f.stop = ['END'];
    f.priority = 'background';
    expect(samplingBody(f, { allowIgnoreEos: true })).toEqual({ temperature: 0.3, max_completion_tokens: 512, stop: ['END'], priority: 'background' });
  });

  it('drops ignore_eos when tools or a schema are on', () => {
    const f = { ...emptySampling(), ignore_eos: true };
    expect(samplingBody(f, { allowIgnoreEos: false })).toEqual({});
    expect(samplingBody(f, { allowIgnoreEos: true })).toEqual({ ignore_eos: true });
  });

  it('uses the spec error copy', () => {
    expect(numError('temperature', '2.5')).toBe('Temperature must be between 0 and 2.');
    expect(numError('temperature', '2')).toBeNull();
    expect(numError('top_p', '0')).toBe('Top P must be between 0 and 1.');
    expect(numError('top_k', '-1')).toBeNull();
    expect(numError('top_k', '1.5')).toBe('Top K must be 0, −1 or a positive integer.');
    expect(numError('repetition_penalty', '0')).toBe('Repetition penalty must be greater than 0.');
    expect(numError('seed', '1.2')).toBe('Seed must be a whole number.');
    expect(numError('max_completion_tokens', '0')).toBe('Max tokens must be at least 1.');
    expect(numError('max_completion_tokens', '200000', 131072)).toBe('Larger than the model’s context (131,072).');
    expect(numError('timeout', '-3')).toBe('Timeout must be a positive number of seconds.');
    expect(numError('temperature', '')).toBeNull();
  });

  it('allows at most four stop sequences, as Splash does (server/frontend.py)', () => {
    expect(stopError(['a', 'b', 'c', 'd'])).toBeNull();
    expect(stopError(['a', 'b', 'c', 'd', 'e'])).toBe('Up to 4 stop sequences.');
  });

  it('flags bad template kwargs and blocks send', () => {
    const f = { ...emptySampling(), kwargs: '[1]' };
    const e = samplingErrors(f);
    expect(e.kwargs).toContain('JSON object');
    expect(hasSamplingErrors(e)).toBe(true);
  });

  it('round-trips a stored chat.sampling', () => {
    const f = samplingForm({ temperature: 0.7, max_tokens: 100, stop: 'X', chat_template_kwargs: { enable_thinking: false } });
    expect(f.num.temperature).toBe('0.7');
    expect(f.num.max_completion_tokens).toBe('100');
    expect(f.stop).toEqual(['X']);
    expect(samplingBody(f, { allowIgnoreEos: true })).toEqual({
      temperature: 0.7,
      max_completion_tokens: 100,
      stop: ['X'],
      chat_template_kwargs: { enable_thinking: false },
    });
  });
});

describe('tools and output (§9.3, §9.4)', () => {
  it('validates tool definitions', () => {
    expect(checkTools('').names).toEqual([]);
    expect(checkTools('{').error).toMatch(/^Line 1, column 2: /);
    expect(checkTools('{}').error).toBe('Must be a JSON array of tool objects.');
    expect(checkTools('[{"type":"x"}]').error).toContain('Tool 1');
    const ok = checkTools('[{"type":"function","function":{"name":"get_weather","parameters":{"type":"object"}}}]');
    expect(ok.error).toBeNull();
    expect(ok.names).toEqual(['get_weather']);
    expect(checkTools('[{"type":"function","function":{"name":"a"}},{"type":"function","function":{"name":"a"}}]').error).toContain('defined twice');
  });

  it('maps tool choice', () => {
    expect(toolChoiceBody('auto', '')).toBe('auto');
    expect(toolChoiceBody('named', 'f')).toEqual({ type: 'function', function: { name: 'f' } });
    expect(toolChoiceError('required', 0)).toBe('Define at least one tool first.');
    expect(toolChoiceError('auto', 0)).toBeNull();
  });

  it('builds response_format and checks schemas', () => {
    expect(outputBody({ kind: 'text', schema: '', name: 'a', strict: true })).toBeNull();
    expect(outputBody({ kind: 'json_object', schema: '', name: 'a', strict: true })).toEqual({ type: 'json_object' });
    expect(outputBody({ kind: 'json_schema', schema: '{"type":"object","properties":{}}', name: 'answer', strict: false })).toEqual({
      type: 'json_schema',
      json_schema: { name: 'answer', schema: { type: 'object', properties: {} }, strict: false },
    });
    expect(schemaError('{"type":"object"}')).toBe('An object schema needs "properties".');
    expect(schemaError('[]')).toBe('The schema must be a JSON object.');
    expect(outputError({ kind: 'json_schema', schema: '{"type":"string"}', name: 'bad name', strict: true })).toContain('Name');
    expect(outputForm({ type: 'json_schema', json_schema: { name: 'x', schema: { type: 'string' }, strict: false } })).toMatchObject({ kind: 'json_schema', name: 'x', strict: false });
  });

  it('checks a structured reply', () => {
    const fmt = { type: 'json_schema', json_schema: { name: 'answer', schema: { type: 'object', properties: { a: {} }, required: ['a'] } } };
    expect(checkResult('{"a":1}', fmt, false)).toEqual({ ok: true, line: 'Valid ✓ · matches schema “answer”' });
    expect(checkResult('{"b":1}', fmt, false).line).toContain('missing “a”');
    expect(checkResult('{"a":', fmt, true).line).toBe('Invalid ✗ · the reply was cut off');
  });
});

describe('conversation list (§3)', () => {
  const chat = (id: string, updated: Date): ChatSummary => ({
    id,
    title: id,
    created_at: updated.toISOString(),
    updated_at: updated.toISOString(),
    profile: 'default',
    message_count: 1,
  });
  it('groups by updated_at in the spec order and drops empty groups', () => {
    const groups = groupChats(
      [chat('older', new Date(2025, 0, 1)), chat('today', new Date(2026, 9, 4, 9)), chat('yday', new Date(2026, 9, 3, 9)), chat('week', new Date(2026, 8, 29))],
      NOW,
    );
    expect(groups.map((g) => g.group)).toEqual(['today', 'yesterday', 'week', 'older']);
  });
  it('formats row times', () => {
    expect(listTime(new Date(2026, 9, 4, 9, 5).toISOString(), NOW)).toBe('09:05');
    expect(listTime(new Date(2026, 9, 3, 9).toISOString(), NOW)).toBe('Yesterday');
    expect(listTime(new Date(2026, 8, 12).toISOString(), NOW)).toBe('12 Sep');
    expect(listTime(new Date(2025, 8, 12).toISOString(), NOW)).toBe('12 Sep 2025');
  });
  it('titles from the first message', () => {
    expect(autoTitle('  Summarise this PDF.  ')).toBe('Summarise this PDF');
    const long = autoTitle('word '.repeat(30));
    expect(long.length).toBeLessThanOrEqual(60);
    expect(long.endsWith(' ')).toBe(false);
  });
});

describe('models (§4)', () => {
  it('builds selector rows from /v1/models with profiles folded in', () => {
    const rows = modelRows([
      { id: 'mlx/A-4bit', loaded: true, max_model_len: 131072, vision: true },
      { id: 'mlx/A-4bit:no-think', root: 'mlx/A-4bit', profile: 'no-think', loaded: true },
      { id: 'u/B-GGUF:Q4', loaded: false, max_model_len: 262144 },
    ]);
    expect(rows).toEqual([
      { id: 'mlx/A-4bit', active: true, context: 131072, estimated: false, vision: true, profiles: ['no-think'] },
      { id: 'u/B-GGUF:Q4', active: false, context: 262144, estimated: true, vision: null, profiles: [] },
    ]);
  });
  it('builds request model ids', () => {
    expect(requestModel('a/b', 'default')).toBe('a/b');
    expect(requestModel('a/b', 'no-think')).toBe('a/b:no-think');
    expect(splitModel('a/b:no-think')).toEqual({ model: 'a/b', profile: 'no-think' });
    expect(splitModel('u/B-GGUF:UD-Q4_K_M')).toEqual({ model: 'u/B-GGUF:UD-Q4_K_M', profile: 'default' });
    expect(shortModel('mlx-community/Qwen3.8-27B-4bit')).toBe('Qwen3.8-27B-4bit');
  });
});

describe('attachments (§7.3)', () => {
  it('counts PDF pages and enforces 64 pages / 64 MiB', () => {
    const pdf = new TextEncoder().encode('%PDF /Type /Pages /Type /Page x /Type /Page y /Type/Page');
    expect(countPdfPages(pdf)).toBe(3);
    expect(countPdfPages(new Uint8Array([1, 2, 3]))).toBeNull();
    expect(attachmentError([{ kind: 'pdf', pages: 40, bytes: 1 }, { kind: 'pdf', pages: 30, bytes: 1 }])).toBe('Too many pages: 70 of 64.');
    expect(attachmentError([{ kind: 'image', pages: null, bytes: 65 * 1024 * 1024 }])).toBe('Attachments are 65.0 MiB; the limit is 64 MiB.');
    expect(attachmentError([{ kind: 'image', pages: null, bytes: 10 }])).toBeNull();
  });
});

describe('errors (§10)', () => {
  const api = (status: number, code: string | null, message = 'x', retry: number | null = null) =>
    new ApiError(status, { message, type: 't', code }, null, retry);
  it('classifies the documented conditions', () => {
    expect(classifyError(api(503, 'model_switch_busy', 'busy', 10), { active: 'm/Active' })).toMatchObject({ kind: 'busy', retryAfter: 10, title: 'Splash is busy serving Active.' });
    expect(classifyError(api(503, 'engine_recovering')).kind).toBe('recovering');
    expect(classifyError(api(500, 'engine_failed')).kind).toBe('failed');
    expect(classifyError(api(400, 'capacity_exhausted')).kind).toBe('capacity');
    expect(classifyError(api(503, 'resource_timeout')).kind).toBe('resource_timeout');
    expect(classifyError(api(504, 'request_timeout')).kind).toBe('request_timeout');
    expect(classifyError(api(503, 'frontend_overloaded')).kind).toBe('queue_full');
    expect(classifyError(api(503, 'mask_timeout')).kind).toBe('mask_timeout');
    expect(classifyError(api(400, null, "this model's chat template does not accept system messages after the first message")).kind).toBe('later_system');
    expect(classifyError(api(404, 'model_not_found'), { model: 'a/b' }).title).toBe('a/b isn’t installed any more.');
    expect(classifyError(api(413, 'attachment_too_large')).kind).toBe('attachment');
    expect(classifyError(api(400, null, 'bad')).kind).toBe('rejected');
    expect(classifyError(new TypeError('Failed to fetch')).kind).toBe('unreachable');
  });
});
