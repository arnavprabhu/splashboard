/**
 * The Playground's RAW view (docs/ui/08 §1.6): every SSE event with the time it arrived, plus
 * keepalive comments, and the assembled PARSED message. Splash's framing: `data: <json>` for
 * OpenAI shapes, `event: <name>` + `data:` for Responses and Messages, `: splash-keepalive`
 * comments, `data: [DONE]` at the end of OpenAI streams (splash/server/server.py `_sse`,
 * `_sse_keepalive`, `_openai_stream`, `_responses_stream`, `_anthropic_stream`).
 */

import type { Shape } from './endpoints';

export type RawKind = 'open' | 'event' | 'comment' | 'body';

export interface RawEvent {
  index: number;
  /** ms since the request started. */
  t: number;
  kind: RawKind;
  /** `event:` name, `data` when absent, `comment`, `open` or `body`. */
  name: string;
  data: string;
}

/** Incremental SSE parser that, unlike api/stream.ts, keeps comment lines. */
export class TimedSseParser {
  private buffer = '';
  private event = '';
  private data: string[] = [];

  push(chunk: string): Array<{ kind: 'event' | 'comment'; name: string; data: string }> {
    this.buffer += chunk;
    const out: Array<{ kind: 'event' | 'comment'; name: string; data: string }> = [];
    let nl: number;
    while ((nl = this.lineEnd()) >= 0) {
      let line = this.buffer.slice(0, nl);
      const skip = this.buffer[nl] === '\r' && this.buffer[nl + 1] === '\n' ? 2 : 1;
      this.buffer = this.buffer.slice(nl + skip);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (line === '') {
        if (this.data.length > 0) out.push({ kind: 'event', name: this.event || 'data', data: this.data.join('\n') });
        this.event = '';
        this.data = [];
        continue;
      }
      if (line.startsWith(':')) {
        out.push({ kind: 'comment', name: 'comment', data: line.slice(1).trim() });
        continue;
      }
      const colon = line.indexOf(':');
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') this.event = value;
      else if (field === 'data') this.data.push(value);
    }
    return out;
  }

  /** Flushes an event that was not followed by a blank line. */
  end(): Array<{ kind: 'event' | 'comment'; name: string; data: string }> {
    const out = this.buffer ? this.push('\n') : [];
    if (this.data.length > 0) out.push({ kind: 'event', name: this.event || 'data', data: this.data.join('\n') });
    this.data = [];
    this.event = '';
    return out;
  }

  private lineEnd(): number {
    const n = this.buffer.indexOf('\n');
    const r = this.buffer.indexOf('\r');
    if (r >= 0 && (n < 0 || r < n)) return r === this.buffer.length - 1 ? -1 : r;
    return n;
  }
}

function parse(data: string): unknown {
  try {
    return JSON.parse(data) as unknown;
  } catch {
    return undefined;
  }
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const nonEmpty = (v: unknown) => (typeof v === 'string' && v.length > 0) || (Array.isArray(v) && v.length > 0);

/** True when this event carries the first generated text (FIRST TOKEN in the footer). */
export function isTokenEvent(shape: Shape, e: Pick<RawEvent, 'kind' | 'name' | 'data'>): boolean {
  if (e.kind !== 'event') return false;
  if (shape === 'responses') return e.name === 'response.output_text.delta' || e.name === 'response.reasoning_summary_text.delta';
  if (shape === 'messages') return e.name === 'content_block_delta';
  const v = parse(e.data);
  if (!isRecord(v) || !Array.isArray(v.choices)) return false;
  const choice = v.choices[0];
  if (!isRecord(choice)) return false;
  if (shape === 'completions') return nonEmpty(choice.text);
  const delta = isRecord(choice.delta) ? choice.delta : null;
  return !!delta && (nonEmpty(delta.content) || nonEmpty(delta.reasoning_content) || nonEmpty(delta.reasoning) || nonEmpty(delta.tool_calls));
}

export interface StreamSummary {
  events: number;
  firstData: number | null;
  firstToken: number | null;
  done: number | null;
}

/** Footer numbers: event count (data events only), first data, first token, last event. */
export function summarize(shape: Shape, rows: readonly RawEvent[], doneAt: number | null): StreamSummary {
  const events = rows.filter((r) => r.kind === 'event' || r.kind === 'body');
  const first = events[0];
  const token = rows.find((r) => isTokenEvent(shape, r));
  return {
    events: events.length,
    firstData: first ? first.t : null,
    firstToken: token ? token.t : null,
    done: doneAt,
  };
}

/** One line for the DATA column. */
export function preview(data: string, max = 120): string {
  const flat = data.replace(/\s+/g, ' ');
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

export function pretty(data: string): string {
  const v = parse(data);
  return v === undefined ? data : JSON.stringify(v, null, 2);
}

// ---------- assembled message (PARSED for streams) ----------

export interface Assembled {
  text: string;
  reasoning: string;
  toolCalls: Array<{ id?: string; name: string; arguments: string }>;
  usage: unknown;
  timings: unknown;
  progress: unknown;
  finish: string | null;
  /** The last `response` object of a Responses stream (has the id for GET/DELETE). */
  response: Record<string, unknown> | null;
  error: unknown;
}

export function emptyAssembled(): Assembled {
  return { text: '', reasoning: '', toolCalls: [], usage: null, timings: null, progress: null, finish: null, response: null, error: null };
}

/** Folds one event into the assembled message; returns the same object for convenience. */
export function assemble(shape: Shape, acc: Assembled, name: string, data: string): Assembled {
  if (data === '[DONE]') return acc;
  const v = parse(data);
  if (!isRecord(v)) return acc;
  if (isRecord(v.error) && !('choices' in v)) acc.error = v;
  if (isRecord(v.prompt_progress)) acc.progress = v.prompt_progress;
  if (shape === 'chat' || shape === 'completions') {
    const choice = Array.isArray(v.choices) && isRecord(v.choices[0]) ? v.choices[0] : null;
    if (choice) {
      if (typeof choice.text === 'string') acc.text += choice.text;
      const delta = isRecord(choice.delta) ? choice.delta : null;
      if (delta) {
        if (typeof delta.content === 'string') acc.text += delta.content;
        if (typeof delta.reasoning_content === 'string') acc.reasoning += delta.reasoning_content;
        else if (typeof delta.reasoning === 'string') acc.reasoning += delta.reasoning;
        if (Array.isArray(delta.tool_calls)) {
          for (const call of delta.tool_calls) {
            if (!isRecord(call)) continue;
            const index = typeof call.index === 'number' ? call.index : acc.toolCalls.length;
            const fn = isRecord(call.function) ? call.function : {};
            const slot = (acc.toolCalls[index] ??= { name: '', arguments: '' });
            if (typeof call.id === 'string') slot.id = call.id;
            if (typeof fn.name === 'string') slot.name += fn.name;
            if (typeof fn.arguments === 'string') slot.arguments += fn.arguments;
          }
        }
      }
      if (typeof choice.finish_reason === 'string') acc.finish = choice.finish_reason;
    }
    if (v.usage) acc.usage = v.usage;
    if (v.timings) acc.timings = v.timings;
    return acc;
  }
  if (shape === 'responses') {
    if (name === 'response.output_text.delta' && typeof v.delta === 'string') acc.text += v.delta;
    if (name === 'response.reasoning_summary_text.delta' && typeof v.delta === 'string') acc.reasoning += v.delta;
    if (isRecord(v.response)) {
      acc.response = v.response;
      const r = v.response;
      if (r.usage) acc.usage = r.usage;
      if (r.timings) acc.timings = r.timings;
      if (typeof r.status === 'string') acc.finish = r.status;
      if (isRecord(r.error)) acc.error = { error: r.error };
      if (isRecord(r.prompt_progress)) acc.progress = r.prompt_progress;
    }
    return acc;
  }
  if (shape === 'messages') {
    if (name === 'content_block_start' && isRecord(v.content_block) && v.content_block.type === 'tool_use') {
      const b = v.content_block;
      acc.toolCalls.push({ ...(typeof b.id === 'string' ? { id: b.id } : {}), name: String(b.name ?? ''), arguments: '' });
    }
    if (name === 'content_block_delta' && isRecord(v.delta)) {
      const d = v.delta;
      if (typeof d.text === 'string') acc.text += d.text;
      if (typeof d.thinking === 'string') acc.reasoning += d.thinking;
      if (typeof d.partial_json === 'string' && acc.toolCalls.length > 0) acc.toolCalls[acc.toolCalls.length - 1]!.arguments += d.partial_json;
    }
    if (name === 'message_start' && isRecord(v.message) && v.message.usage) acc.usage = v.message.usage;
    if (name === 'message_delta') {
      if (v.usage) acc.usage = { ...(isRecord(acc.usage) ? acc.usage : {}), ...(v.usage as object) };
      if (isRecord(v.delta) && typeof v.delta.stop_reason === 'string') acc.finish = v.delta.stop_reason;
    }
    if (isRecord(v.timings)) acc.timings = v.timings;
    if (name === 'error') acc.error = v;
    return acc;
  }
  return acc;
}

/** The id of a Responses object in a parsed body or assembled stream (prefills GET/DELETE). */
export function responseIdOf(value: unknown): string | null {
  if (isRecord(value) && typeof value.id === 'string' && value.id.startsWith('resp_')) return value.id;
  return null;
}
