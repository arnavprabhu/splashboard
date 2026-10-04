/**
 * Chat page types. The stored document is the manager's `Chat` (docs/api.md §8, SPEC §15.2);
 * these narrow the free-form parts (`meta`, `tool_calls`, `sampling`) that the page writes.
 */
import type { Chat, ChatAttachment, ChatMessage, ChatSummary } from '../../api/models';

export type { Chat, ChatAttachment, ChatMessage, ChatSummary };
export type Role = ChatMessage['role'];

/** OpenAI tool call as stored on assistant messages. */
export interface ToolCall {
  id: string;
  type: 'function';
  function: { name: string; arguments: string };
}

/** Order of text and tool calls inside one assistant reply ("text after a call", SPEC §10.5). */
export type Segment = { kind: 'text'; text: string } | { kind: 'tool'; index: number };

/** `usage` from the final stream chunk (splash/server/metrics.py usage_dict). */
export interface Usage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  prompt_tokens_details?: { cached_tokens?: number } | null;
  completion_tokens_details?: { reasoning_tokens?: number } | null;
}

/** `timings` on the finish chunk (splash/server/metrics.py timings_dict). */
export interface Timings {
  prompt_n?: number;
  prompt_ms?: number;
  prompt_per_second?: number;
  predicted_n?: number;
  predicted_ms?: number;
  predicted_per_second?: number;
  cache_n?: number;
}

/** `prompt_progress` on empty-delta chunks (splash/server/backend.py). */
export interface PromptProgress {
  total: number;
  cache: number;
  processed: number;
  time_ms: number | null;
}

export type FinishReason = 'stop' | 'length' | 'tool_calls' | 'stopped' | 'error' | 'disconnected' | string;

export type ToolSource = 'manual' | 'mcp' | 'skipped' | 'unknown';

/** What the page keeps in `ChatMessage.meta` (extra keys are allowed by the manager). */
export interface MessageMeta {
  usage?: Usage | null;
  timings?: Timings | null;
  ttft_ms?: number | null;
  finish_reason?: FinishReason | null;
  profile?: string | null;
  model?: string | null;
  thinking_ms?: number | null;
  segments?: Segment[];
  /** Estimated output tokens after Stop (text length ÷ 4). */
  est_out?: number;
  /** Assistant replies that ran with a response_format: the format, for the result check. */
  response_format?: Record<string, unknown> | null;
  /** Tool messages: where the result came from. */
  tool?: { source: ToolSource; server?: string | null; duration_ms?: number | null; is_error?: boolean; auto?: boolean; name?: string };
  /** Fields the proxy injected (sampling defaults / profile), from the usage row (G4). */
  injected?: Record<string, unknown> | null;
  /** User messages: page counts per attachment (ChatAttachment has no pages field). */
  attachment_pages?: Array<number | null>;
}

export function metaOf(m: ChatMessage): MessageMeta {
  return (m.meta ?? {}) as MessageMeta;
}

export function toolCallsOf(m: ChatMessage): ToolCall[] {
  return (m.tool_calls ?? []) as unknown as ToolCall[];
}

/** Plain text of a message's content (string, or the text parts of a part list). */
export function textOf(m: Pick<ChatMessage, 'content'>): string {
  if (typeof m.content === 'string') return m.content;
  return m.content
    .map((p) => (p && (p as { type?: unknown }).type === 'text' ? String((p as { text?: unknown }).text ?? '') : ''))
    .join('');
}

/** An attachment in the composer, before and after upload. */
export interface DraftAttachment {
  id: string;
  kind: 'image' | 'pdf';
  name: string;
  mime: string;
  bytes: number;
  /** base64 data URL, for the request (and for local-only storage). */
  dataUrl: string;
  pages: number | null;
  /** Larger than max_image_pixels: Splash resizes it. */
  resize: boolean;
  /** `attachments/<sha>.<ext>` once uploaded to the manager. */
  file?: string;
}

/** Stored attachment, plus the inline data URL when the manager can't store files yet. */
export type StoredAttachment = ChatAttachment & { data_url?: string };

export type ResponseFormatKind = 'text' | 'json_object' | 'json_schema';

/** A `/v1/models` entry (SPEC §7.3, manager proxy/pipeline.py models_list). */
export interface ModelEntry {
  id: string;
  root?: string;
  loaded?: boolean;
  profile?: string;
  max_model_len?: number;
  context_length?: number;
  vision?: boolean;
  input_modalities?: string[];
}
