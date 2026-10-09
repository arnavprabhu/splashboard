import { afterEach, describe, expect, it, vi } from 'vitest';
import { stagePercentileMs } from '../src/routes/status/logic';
import { answerView, detailKey, newQuestion, questionBody, questionErrors, questionsFromBody, semifErrors, shortHash, systemOneBody } from '../src/routes/tools/judgments';
import { SYSTEMONE_EXAMPLE } from '../src/routes/tools/endpoints';
import { overlayErrors } from '../src/routes/settings/OverlayEditor';
import { searchFields } from '../src/routes/settings';
import { consequence, sizeText } from '../src/routes/settings/DataPrivacy';
import { updateLine } from '../src/routes/settings/About';
import { traceTime } from '../src/routes/logs-diagnostics';
import { parseDivider } from '../src/routes/logs/model';
import { fingerprintRows } from '../src/routes/model-settings';
import { piecesFor } from '../src/routes/tools-tokenizer';
import type { SchemaField } from '../src/api/models';

describe('latency percentiles from cumulative buckets (decision T5)', () => {
  const h = { count: 10, sum: 3, buckets: { '0.05': 2, '0.1': 5, '0.5': 9, '1.0': 10, '+Inf': 10 } };
  it('takes the upper bound of the first bucket reaching q·count, in ms', () => {
    expect(stagePercentileMs(h, 0.5)).toBe(100);
    expect(stagePercentileMs(h, 0.95)).toBe(1000);
    expect(stagePercentileMs({ count: 0, buckets: {} }, 0.5)).toBeNull();
    expect(stagePercentileMs({ count: 2, buckets: { '0.1': 0, '+Inf': 2 } }, 0.5)).toBe(100);
  });
});

describe('System One builder (docs/ui/08 §3.2)', () => {
  it('round-trips the DEVELOPMENT.md example', () => {
    const qs = questionsFromBody(SYSTEMONE_EXAMPLE)!;
    expect(qs.map((q) => q.type)).toEqual(['noul', 'choice', 'score']);
    const body = systemOneBody('m/x', SYSTEMONE_EXAMPLE.state, qs);
    expect(body).toEqual({ model: 'm/x', ...SYSTEMONE_EXAMPLE });
  });
  it('omits empty instructions and validates keys and domains', () => {
    const q = newQuestion('choice', []);
    expect(questionBody({ ...q, instructions: '' })).not.toHaveProperty('instructions');
    const dup = [q, { ...newQuestion('noul', []), key: q.key }];
    expect(Object.values(questionErrors(dup))).toContain('Keys must be unique.');
    expect(Object.values(questionErrors([{ ...q, labels: [{ label: 'a', description: '' }, { label: 'a', description: '' }] }]))).toContain('Labels must be unique.');
    expect(questionsFromBody({ questions: { a: { type: 'bogus' } } })).toBeNull();
  });
  it('maps a 422 loc onto a question key', () => {
    expect(detailKey(['body', 'questions', 'department', 'criteria'])).toBe('department');
    expect(detailKey(['body', 'state'])).toBeNull();
  });
  it('renders answers with one winner per question and 1-based scores', () => {
    const choice = answerView(undefined, { type: 'choice', choice: 'billing', probabilities: { billing: 0.84, technical: 0.1, sales: 0.06 }, confidence: 0.78 });
    expect(choice.head).toBe('Answer billing');
    expect(choice.bars.filter((b) => b.winner).map((b) => b.label)).toEqual(['billing']);
    const score = answerView(undefined, { type: 'score', score: 1.71, legend: ['No urgency', 'This week', 'Today'], probabilities: { '0': 0.04, '1': 0.21, '2': 0.75 } });
    expect(score.head).toBe('Score 2.71 / 3');
    expect(score.bars[2]).toMatchObject({ label: '3 Today', winner: true });
    const noul = answerView(undefined, { type: 'noul', noul: 0.91 });
    expect(noul.head).toBe('Answer true');
  });
  it('checks SemIf rows', () => {
    expect(semifErrors({ id: 'a', state: 's', question: 'q', options: [{ id: 'x', description: '' }] })).toBe('Add at least two options.');
    expect(semifErrors({ id: 'a', state: 's', question: 'q', options: [{ id: 'x', description: '' }, { id: 'x', description: '' }] })).toBe('Option ids must be unique.');
    expect(shortHash('3f9cabcdefa1')).toBe('3f9c…a1');
  });
});

describe('settings helpers', () => {
  it('validates overlays with the settings wording', () => {
    expect(overlayErrors({ temperature: 3 }).temperature).toBeTruthy();
    expect(overlayErrors({ temperature: 0.7, top_k: -1 })).toEqual({});
    expect(overlayErrors({}, { chat_template_kwargs: '[1]' }).chat_template_kwargs).toBeTruthy();
  });
  it('searches fields by label, key, flag and help', () => {
    const f = (key: string, label: string, flag: string | null, help: string) => ({ key, label, flag, help, section: 's' }) as unknown as SchemaField;
    const fields = [f('serve.max_cache_disk', 'SSD cache', '--max-cache-disk', 'Disk tier'), f('server.port', 'Port', '--port', 'Where it listens')];
    expect(searchFields(fields, 'cache-disk').map((x) => x.key)).toEqual(['serve.max_cache_disk']);
    expect(searchFields(fields, 'listens').map((x) => x.key)).toEqual(['server.port']);
    expect(searchFields(fields, '  ')).toEqual([]);
  });
  it('states sizes and consequences in the clear confirmation', () => {
    expect(sizeText(null)).toBe('—');
    expect(sizeText(1024 ** 3)).toBe('1 GB');
    expect(consequence('kv_cache')).toContain('Stops the engine first');
    expect(consequence('responses')).toContain('Restarts the engine');
  });
  it('describes the engine update state', () => {
    expect(updateLine({ available: true, version: '1.2.1' })).toBe('Splash 1.2.1 available');
    expect(updateLine(null)).toBe('Not checked yet');
  });
  it('shows crash trace times', () => {
    expect(traceTime(new Date(2026, 9, 3, 14, 5).toISOString())).toBe('2026-10-03 14:05');
    expect(traceTime(null)).toBe('—');
  });
});

import { contextGate, delta } from '../src/routes/tools-benchmark';

describe('benchmark compare (docs/ui/08 §4.4)', () => {
  it('shows deltas against the first run and inverts TTFT', () => {
    expect(delta(100, 112.4, false)).toEqual({ text: '+12.4 %', worse: false });
    expect(delta(100, 91.9, false)).toEqual({ text: '−8.1 %', worse: true });
    expect(delta(100, 120, true)).toEqual({ text: '+20.0 %', worse: true });
    expect(delta(null, 1, false)).toBeNull();
  });
  it('gates prefill sizes on the loaded context (D-08-6)', () => {
    expect(contextGate(32768)).toBeGreaterThan(32768);
    expect(contextGate(32768)).toBeLessThanOrEqual(34000);
  });
});

import { argvToFlags } from '../src/routes/settings/ExtraFlags';

describe('other engine options (docs/ui/05 §7)', () => {
  it('turns engine.extra_flags argv into rows', () => {
    expect(argvToFlags(['--new-thing', '42', '--other-switch'])).toEqual([
      { flag: '--new-thing', value: '42' },
      { flag: '--other-switch', value: null },
    ]);
    expect(argvToFlags([])).toEqual([]);
  });
});

import { EMPTY, draftError, draftServer, editDraft, importMcpJson, mergeSecrets, parsePairs, secretRows } from '../src/routes/settings/McpServers';

describe('MCP servers editor (docs/ui/05 §3.11)', () => {
  const base = { ...EMPTY, name: 'weather', transport: 'stdio' as const, command: 'npx', args: 'weather-mcp\n--port\n9', env: 'KEY=v', url: '', headers: '' };
  it('validates names, commands and URLs', () => {
    expect(draftError(base, [])).toBeNull();
    expect(draftError(base, ['weather'])).toBe('A server with this name exists.');
    expect(draftError({ ...base, command: '' }, [])).toBe('Enter the command that starts the server.');
    expect(draftError({ ...base, transport: 'http', url: 'ftp://x' }, [])).toBe('Enter an http(s) URL.');
  });
  it('builds stdio and http entries', () => {
    expect(draftServer(base)).toEqual({ command: 'npx', args: ['weather-mcp', '--port', '9'], env: { KEY: 'v' }, enabled: true, always_allow: false });
    expect(draftServer({ ...base, transport: 'http', url: 'https://h/mcp', headers: 'Authorization: Bearer x' })).toEqual({ url: 'https://h/mcp', headers: { Authorization: 'Bearer x' }, enabled: true, always_allow: false });
    expect(parsePairs('A=1\nnope\nB = 2')).toEqual({ A: '1', B: '2' });
  });
  it('imports a Claude-style mcp.json', () => {
    expect(importMcpJson('{"mcpServers":{"w":{"command":"npx","args":["a"]}}}')).toEqual({ w: { command: 'npx', args: ['a'], env: {}, enabled: true, always_allow: false } });
    expect(importMcpJson('nope')).toBeNull();
  });
});

describe('MCP secrets are write-only (D43)', () => {
  const key = { secret: true as const, masked: 'sk-…••••a1b2' };
  const server = { command: 'npx', args: ['gh-mcp'], env: { GITHUB_TOKEN: key, ORG: { secret: true as const, masked: '••••corp' } }, enabled: false, always_allow: true };
  it('sends untouched values back as the same object, replaced ones as plain strings', () => {
    const rows = secretRows(server.env);
    expect(mergeSecrets(rows, {})).toEqual(server.env);
    rows[0]!.replace = 'ghp_new';
    rows[1]!.removed = true;
    expect(mergeSecrets(rows, { EXTRA: 'x' })).toEqual({ GITHUB_TOKEN: 'ghp_new', EXTRA: 'x' });
  });
  it('never sends masked text as a value', () => {
    const rows = secretRows(server.env);
    rows[0]!.replace = key.masked;
    expect(mergeSecrets(rows, { NEW: '••••' })).toEqual(server.env);
    const d = editDraft('gh', server);
    d.envRows[0]!.replace = key.masked;
    expect(draftError(d, ['gh'])).toBe('That is the masked value. Paste the real value, or keep the saved one.');
  });
  it('edits keep the name, flags and saved secrets', () => {
    const d = editDraft('gh', server);
    expect(draftError(d, ['gh'])).toBeNull();
    expect(draftServer(d)).toEqual(server);
  });
  it('import drops masked objects and masked text', () => {
    expect(importMcpJson(JSON.stringify({ mcpServers: { w: { command: 'x', env: { A: key, B: 'b', C: 'x••••y' } } } }))!.w!.env).toEqual({ B: 'b' });
  });
});

describe('model fingerprints on Info (GET /models/{id} fingerprints)', () => {
  it('shows the recorded values, or "load once" when nothing is recorded', () => {
    const rows = fingerprintRows({ build_id: 'b-42', loaded_model_layout_sha256: 'abc', target_model_sha256: 'def', kv_format: 'int8', kv_quantization: 'symmetric_int8', max_context: 131072, recorded_at: null } as never);
    expect(rows.map((r) => r.key)).toEqual(['build', 'layout', 'target', 'kv', 'ctx', 'recorded']);
    expect(rows[4]!.value).toBe('128K');
    const empty = fingerprintRows(null);
    expect(empty.every((r) => typeof r.value === 'string')).toBe(true);
  });
});

describe('tokenizer pieces (POST /tokenizer/pieces)', () => {
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  afterEach(() => vi.restoreAllMocks());
  it('maps decoded text per id and keeps the vocab piece', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(200, { model: 'm', pieces: [{ id: 785, piece: 'The', text: 'The' }, { id: 3974, piece: 'Ġquick', text: ' quick' }] }));
    const out = await piecesFor([785, 3974], 'm');
    expect(out.ok).toBe(true);
    expect(out.tokens[1]).toMatchObject({ id: 3974, piece: ' quick', vocab: 'Ġquick', special: false });
  });
  it('falls back to IDs only on pieces_unavailable', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(503, { error: { message: 'x', type: 'x', code: 'pieces_unavailable' } }));
    const out = await piecesFor([1, 2], undefined);
    expect(out).toEqual({ ok: false, tokens: [{ id: 1, piece: null, special: false }, { id: 2, piece: null, special: false }] });
  });
});

describe('engine session dividers', () => {
  it('reads the current marker and the one written before the D62 rename', () => {
    for (const name of ['Splashboard', 'Splash GUI']) {
      expect(parseDivider(`=== ${name}: engine session started · a/b · splash serve`)).toMatchObject({ kind: 'start', model: 'a/b', command: 'splash serve' });
      expect(parseDivider(`=== ${name}: engine session ended · stopped · exit 0`)).toMatchObject({ kind: 'stop', reason: 'stopped', exit: '0' });
    }
  });
});
