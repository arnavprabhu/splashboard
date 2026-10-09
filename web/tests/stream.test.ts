import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../src/api/client';
import { postStream, SseParser } from '../src/api/stream';

function streamResponse(chunks: string[], init: ResponseInit = { status: 200 }): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(body, { ...init, headers: { 'Content-Type': 'text/event-stream' } });
}

describe('SseParser', () => {
  it('handles events split across chunks, comments, event names and multi-line data', () => {
    const p = new SseParser();
    expect(p.push(': splash-keepalive\n\nda')).toEqual([]);
    expect(p.push('ta: {"a":1}\n')).toEqual([]);
    expect(p.push('\n')).toEqual([{ event: 'message', data: '{"a":1}' }]);
    expect(p.push('event: message_start\ndata: line1\ndata: line2\nid: 7\n\n')).toEqual([
      { event: 'message_start', data: 'line1\nline2', id: '7' },
    ]);
  });

  it('handles CRLF, including a CR/LF pair split across chunks', () => {
    const p = new SseParser();
    expect(p.push('data: x\r')).toEqual([]);
    expect(p.push('\n\r\n')).toEqual([{ event: 'message', data: 'x' }]);
    expect(p.push('data:no-space\r\rdata: y\n\n')).toEqual([
      { event: 'message', data: 'no-space' },
      { event: 'message', data: 'y' },
    ]);
  });

  it('flushes a trailing event at end of stream', () => {
    const p = new SseParser();
    p.push('data: tail');
    expect(p.end()).toEqual([{ event: 'message', data: 'tail' }]);
  });
});

describe('postStream', () => {
  it('yields parsed chunks and stops at [DONE]', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse([
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
        ': splash-keepalive\n\n',
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\ndata: [DONE]\n\n',
        'data: {"ignored":true}\n\n',
      ]),
    );
    const out: unknown[] = [];
    for await (const m of postStream('/v1/chat/completions', { stream: true })) out.push(m.data);
    expect(out).toEqual([{ choices: [{ delta: { content: 'Hel' } }] }, { choices: [{ delta: { content: 'lo' } }] }]);
    const init = fetchMock.mock.calls[0]![1]!;
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>).Accept).toBe('text/event-stream');
  });

  it('throws ApiError for an in-stream Splash error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse([
        'data: {"choices":[]}\n\n',
        'data: {"error":{"message":"Engine recovering","type":"server_error","code":"engine_recovering"}}\n\n',
        'data: [DONE]\n\n',
      ]),
    );
    const seen: unknown[] = [];
    const err = await (async () => {
      for await (const m of postStream('/v1/chat/completions', {})) seen.push(m);
    })().catch((e: unknown) => e);
    expect(seen).toHaveLength(1);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ code: 'engine_recovering', message: 'Engine recovering' });
  });

  it('throws for Anthropic error events', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse(['event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n']),
    );
    await expect(
      (async () => {
        for await (const _ of postStream('/v1/messages', {})) void _;
      })(),
    ).rejects.toMatchObject({ type: 'overloaded_error', message: 'busy' });
  });

  it('throws for a failed Responses stream', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse([
        'event: response.created\ndata: {"type":"response.created","sequence_number":0}\n\n',
        'event: response.failed\ndata: {"type":"response.failed","sequence_number":1,"response":{"status":"failed","error":{"type":"server_error","code":"engine_recovering","message":"Engine is recovering"}}}\n\n',
      ]),
    );
    const seen: string[] = [];
    await expect(
      (async () => {
        for await (const m of postStream('/v1/responses', {})) seen.push(m.event);
      })(),
    ).rejects.toMatchObject({ code: 'engine_recovering', message: 'Engine is recovering' });
    expect(seen).toEqual(['response.created']);
  });

  it('keeps Anthropic named events', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse(['event: message_start\ndata: {"type":"message_start"}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n']),
    );
    const events: string[] = [];
    for await (const m of postStream('/v1/messages', {})) events.push(m.event);
    expect(events).toEqual(['message_start', 'message_stop']);
  });

  it('throws ApiError for a non-2xx response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: { message: 'too long', type: 'invalid_request_error', code: 'context_length_exceeded' } }), {
        status: 400,
      }),
    );
    await expect(
      (async () => {
        for await (const _ of postStream('/v1/chat/completions', {})) void _;
      })(),
    ).rejects.toMatchObject({ status: 400, code: 'context_length_exceeded' });
  });

  it('stops when aborted', async () => {
    const controller = new AbortController();
    const enc = new TextEncoder();
    let push: ((s: string) => void) | null = null;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        push = (s) => c.enqueue(enc.encode(s));
      },
    });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body, { status: 200 }));
    const seen: unknown[] = [];
    const run = (async () => {
      for await (const m of postStream('/v1/chat/completions', {}, { signal: controller.signal })) {
        seen.push(m.data);
        controller.abort();
      }
    })();
    await new Promise((r) => setTimeout(r, 0));
    push!('data: {"n":1}\n\n');
    await run.catch(() => undefined);
    expect(seen).toEqual([{ n: 1 }]);
  });
});
