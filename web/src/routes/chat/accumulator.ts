/**
 * Folds OpenAI chat-completion stream chunks, as Splash sends them, into one assistant reply.
 * Verified against splash/server/server.py `_openai_stream` and api_shapes.py `stream_chunk`:
 *
 * - `{"delta": {"role": "assistant", "content": ""}}` opens the stream;
 * - text arrives as `delta.content`, reasoning as `delta.reasoning_content` (output.py);
 * - tool calls arrive as `delta.tool_calls: [{index, id?, type?, function: {name?, arguments?}}]`
 *   with the arguments split across chunks of the same `index` (output.py `_emit_argument`);
 * - with `return_progress: true`, empty-delta chunks carry `prompt_progress
 *   {total, cache, processed, time_ms}` (backend.py), monotone, before any output;
 * - the finish chunk carries `finish_reason` and `timings` (metrics.py timings_dict);
 * - with `stream_options.include_usage`, a last chunk with `choices: []` carries `usage`.
 */
import type { PromptProgress, Segment, Timings, ToolCall, Usage } from './types';

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

export interface AccSnapshot {
  content: string;
  reasoning: string;
  segments: Segment[];
  toolCalls: ToolCall[];
  progress: PromptProgress | null;
  /** True once the first output delta arrived (the prefill bar goes away). */
  outputStarted: boolean;
  usage: Usage | null;
  timings: Timings | null;
  finishReason: string | null;
  ttftMs: number | null;
  thinkingMs: number | null;
  /** Reasoning still streaming (no content/tool delta yet). */
  thinking: boolean;
  reasoningStartedAt: number | null;
}

export class StreamAccumulator {
  content = '';
  reasoning = '';
  segments: Segment[] = [];
  toolCalls: ToolCall[] = [];
  progress: PromptProgress | null = null;
  usage: Usage | null = null;
  timings: Timings | null = null;
  finishReason: string | null = null;
  readonly startedAt: number;
  firstOutputAt: number | null = null;
  reasoningStartAt: number | null = null;
  reasoningEndAt: number | null = null;
  /** Chunk count, for the "every 2 s" persistence and tests. */
  chunks = 0;
  private toolSlots = new Map<number, number>();

  constructor(startedAt = Date.now()) {
    this.startedAt = startedAt;
  }

  /** Applies one parsed `data:` payload. Unknown shapes are ignored. */
  push(chunk: unknown, now = Date.now()): void {
    if (!isRecord(chunk)) return;
    this.chunks += 1;
    const progress = chunk.prompt_progress;
    if (isRecord(progress) && this.firstOutputAt === null) this.applyProgress(progress);
    if (isRecord(chunk.usage)) this.usage = chunk.usage as Usage;
    if (isRecord(chunk.timings)) this.timings = chunk.timings as Timings;
    const choices = Array.isArray(chunk.choices) ? chunk.choices : [];
    for (const choice of choices) {
      if (!isRecord(choice)) continue;
      const delta = isRecord(choice.delta) ? choice.delta : null;
      if (delta) this.applyDelta(delta, now);
      if (typeof choice.finish_reason === 'string' && choice.finish_reason) {
        this.finishReason = choice.finish_reason;
        this.endReasoning(now);
      }
    }
  }

  private applyProgress(p: Record<string, unknown>): void {
    const total = num(p.total);
    const processed = num(p.processed);
    if (total === null || processed === null) return;
    const cache = num(p.cache) ?? 0;
    // The engine guarantees monotone progress; clamp anyway.
    const prev = this.progress;
    this.progress = {
      total,
      cache,
      processed: prev ? Math.max(prev.processed, processed) : processed,
      time_ms: num(p.time_ms),
    };
  }

  private markOutput(now: number): void {
    if (this.firstOutputAt === null) this.firstOutputAt = now;
  }

  private endReasoning(now: number): void {
    if (this.reasoningStartAt !== null && this.reasoningEndAt === null) this.reasoningEndAt = now;
  }

  private applyDelta(delta: Record<string, unknown>, now: number): void {
    const reasoning = typeof delta.reasoning_content === 'string' ? delta.reasoning_content : typeof delta.reasoning === 'string' ? delta.reasoning : '';
    if (reasoning) {
      this.markOutput(now);
      if (this.reasoningStartAt === null) this.reasoningStartAt = now;
      this.reasoning += reasoning;
    }
    const text = typeof delta.content === 'string' ? delta.content : '';
    if (text) {
      this.markOutput(now);
      this.endReasoning(now);
      this.content += text;
      const last = this.segments[this.segments.length - 1];
      if (last && last.kind === 'text') last.text += text;
      else this.segments.push({ kind: 'text', text });
    }
    if (Array.isArray(delta.tool_calls)) {
      for (const raw of delta.tool_calls) {
        if (!isRecord(raw)) continue;
        this.markOutput(now);
        this.endReasoning(now);
        this.applyToolDelta(raw);
      }
    }
  }

  private applyToolDelta(raw: Record<string, unknown>): void {
    const index = num(raw.index) ?? this.toolCalls.length;
    let slot = this.toolSlots.get(index);
    if (slot === undefined) {
      slot = this.toolCalls.length;
      this.toolSlots.set(index, slot);
      this.toolCalls.push({ id: '', type: 'function', function: { name: '', arguments: '' } });
      this.segments.push({ kind: 'tool', index: slot });
    }
    const call = this.toolCalls[slot]!;
    if (typeof raw.id === 'string' && raw.id) call.id = raw.id;
    const fn = isRecord(raw.function) ? raw.function : null;
    if (fn) {
      if (typeof fn.name === 'string' && fn.name) call.function.name = call.function.name ? call.function.name + fn.name : fn.name;
      if (typeof fn.arguments === 'string') call.function.arguments += fn.arguments;
    }
  }

  get outputStarted(): boolean {
    return this.firstOutputAt !== null;
  }

  get ttftMs(): number | null {
    return this.firstOutputAt === null ? null : Math.max(0, this.firstOutputAt - this.startedAt);
  }

  thinkingMs(now = Date.now()): number | null {
    if (this.reasoningStartAt === null) return null;
    return Math.max(0, (this.reasoningEndAt ?? now) - this.reasoningStartAt);
  }

  snapshot(now = Date.now()): AccSnapshot {
    return {
      content: this.content,
      reasoning: this.reasoning,
      segments: this.segments.map((s) => ({ ...s })),
      toolCalls: this.toolCalls.map((c) => ({ ...c, function: { ...c.function } })),
      progress: this.progress,
      outputStarted: this.outputStarted,
      usage: this.usage,
      timings: this.timings,
      finishReason: this.finishReason,
      ttftMs: this.ttftMs,
      thinkingMs: this.thinkingMs(now),
      thinking: this.reasoningStartAt !== null && this.reasoningEndAt === null,
      reasoningStartedAt: this.reasoningStartAt,
    };
  }
}

/** Fills missing call ids (the engine always sends one; manual results need a stable id). */
export function ensureCallIds(calls: ToolCall[], prefix: string): ToolCall[] {
  return calls.map((c, i) => (c.id ? c : { ...c, id: `call_${prefix}_${i}` }));
}

/** Rough token estimate after Stop (usage is unknown): characters ÷ 4. */
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}
