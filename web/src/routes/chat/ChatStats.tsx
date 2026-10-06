/**
 * The chat's live numbers: context used (under the composer) and the 2 × 2 stat tiles pinned
 * under the side panel (prefill, token generation, TTFT, duration).
 */
import { useEffect, useState } from 'preact/hooks';
import type { LiveMetrics } from '../../api/models';
import { Tooltip } from '../../components/Tooltip';
import { formatCount, formatTokPerSec } from '../../lib/format';
import { t } from '../../strings/chat';
import { estimateTokens } from './accumulator';
import { metaOf, type ChatMessage } from './types';

const DASH = '—';

function streamingText(m: ChatMessage): string {
  return `${m.reasoning ?? ''}${typeof m.content === 'string' ? m.content : ''}`;
}

/** The streaming reply: the last message on the path while busy, when it is an assistant reply. */
function streamingReply(thread: ChatMessage[], busy: boolean): ChatMessage | null {
  const last = thread[thread.length - 1];
  return busy && last?.role === 'assistant' ? last : null;
}

/**
 * Tokens the conversation takes up now: the last finished reply's prompt + output; while a reply
 * streams, the prompt the engine reports plus the output so far.
 */
export function contextUsed(thread: ChatMessage[], busy: boolean, promptTotal: number | null): number {
  let used = 0;
  for (let i = thread.length - 1; i >= 0; i--) {
    const u = thread[i]!.role === 'assistant' ? metaOf(thread[i]!).usage : null;
    if (u?.prompt_tokens != null) {
      used = u.prompt_tokens + (u.completion_tokens ?? 0);
      break;
    }
  }
  const streaming = streamingReply(thread, busy);
  if (streaming && metaOf(streaming).usage?.prompt_tokens == null) {
    used = Math.max(used, promptTotal ?? 0) + (promptTotal ? estimateTokens(streamingText(streaming)) : 0);
  }
  return used;
}

function pctText(pct: number): string {
  return pct >= 10 ? String(Math.round(pct)) : String(Number(pct.toFixed(1)));
}

export interface ContextMeterProps {
  thread: ChatMessage[];
  busy: boolean;
  promptTotal: number | null;
  context: number | null;
  contextEstimated: boolean;
}

/** "1,942 tokens · 6%", with "Context used: X of Y tokens" on hover and focus. */
export function ContextMeter(p: ContextMeterProps) {
  const used = contextUsed(p.thread, p.busy, p.promptTotal);
  const total = p.context;
  const pct = total ? Math.min(100, (used / total) * 100) : null;
  const usedText = used.toLocaleString('en-US');
  const tip = total
    ? t(p.contextEstimated ? 'chat.stats.tip.ctx_est' : 'chat.stats.tip.ctx', { used: usedText, total: total.toLocaleString('en-US') })
    : t('chat.stats.tip.ctx_unknown', { used: usedText });
  return (
    <Tooltip text={tip}>
      <span class="meta tnum chat-ctx" tabIndex={0} data-testid="chat-context">
        {pct === null ? t('chat.stats.ctx_plain', { used: usedText }) : t('chat.stats.ctx', { used: usedText, pct: pctText(pct) })}
      </span>
    </Tooltip>
  );
}

export interface TileValues {
  prefill: number | null;
  promptTokens: number | null;
  decode: number | null;
  outTokens: number | null;
  ttftMs: number | null;
  cached: number | null;
  durationMs: number | null;
}

/**
 * Live while a reply runs (the engine's sample for the rates, the stream for the rest), else the
 * last reply's own timings and usage.
 */
export function tileValues(thread: ChatMessage[], busy: boolean, live: LiveMetrics | null, promptTotal: number | null, startedAt: number | null, now: number): TileValues {
  const streaming = streamingReply(thread, busy);
  if (streaming) {
    const meta = metaOf(streaming);
    return {
      prefill: live?.throughput?.prefill_tps ?? null,
      promptTokens: promptTotal,
      decode: live?.throughput?.decode_tps ?? null,
      outTokens: streamingText(streaming) ? estimateTokens(streamingText(streaming)) : null,
      ttftMs: meta.ttft_ms ?? null,
      cached: null,
      durationMs: startedAt === null ? null : Math.max(0, now - startedAt),
    };
  }
  const last = [...thread].reverse().find((m) => m.role === 'assistant');
  const meta = last ? metaOf(last) : null;
  const tm = meta?.timings;
  const engineMs = tm?.prompt_ms != null && tm?.predicted_ms != null ? tm.prompt_ms + tm.predicted_ms : null;
  return {
    prefill: tm?.prompt_per_second ?? null,
    promptTokens: meta?.usage?.prompt_tokens ?? tm?.prompt_n ?? null,
    decode: tm?.predicted_per_second ?? null,
    outTokens: meta?.usage?.completion_tokens ?? tm?.predicted_n ?? meta?.est_out ?? null,
    ttftMs: meta?.ttft_ms ?? null,
    cached: meta?.usage?.prompt_tokens_details?.cached_tokens || null,
    durationMs: meta?.duration_ms ?? engineMs,
  };
}

function secs(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return DASH;
  const s = ms / 1000;
  return s < 10 ? s.toFixed(2) : s.toFixed(1);
}

function Tile({ label, value, side, tip, id }: { label: string; value: string; side: string | null; tip: string; id: string }) {
  return (
    <div class="chat-tile" title={tip} data-testid={`chat-tile-${id}`}>
      <dt class="chat-tile-label">{label}</dt>
      <dd class="chat-tile-value tnum">{value}</dd>
      {side && <dd class="chat-tile-side meta tnum">{side}</dd>}
    </div>
  );
}

export interface StatTilesProps {
  thread: ChatMessage[];
  busy: boolean;
  live: LiveMetrics | null;
  promptTotal: number | null;
  /** When the running reply was sent (Date.now()), for the live duration. */
  startedAt: number | null;
}

/** 2 × 2 tiles divided by 1px rules: PREFILL · TOKEN GEN / TTFT · DURATION. */
export function StatTiles(p: StatTilesProps) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!p.busy) return;
    const id = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(id);
  }, [p.busy]);
  const v = tileValues(p.thread, p.busy, p.live, p.promptTotal, p.startedAt, p.busy ? now : Date.now());
  const tok = (n: number | null) => (n === null ? null : t('chat.stats.tok', { n: formatCount(n) }));
  return (
    <section class="chat-tiles" aria-label={p.busy ? t('chat.stats.live_label') : t('chat.stats.label')}>
      <dl class="chat-tiles-grid">
        <Tile id="prefill" label={t('chat.stats.prefill')} value={formatTokPerSec(v.prefill, { unit: false })} side={tok(v.promptTokens)} tip={t('chat.stats.tip.prefill')} />
        <Tile id="decode" label={t('chat.stats.decode')} value={formatTokPerSec(v.decode, { unit: false })} side={tok(v.outTokens)} tip={t('chat.stats.tip.decode')} />
        <Tile id="ttft" label={t('chat.stats.ttft')} value={secs(v.ttftMs)} side={v.cached ? t('chat.stats.cached', { n: formatCount(v.cached) }) : null} tip={t('chat.stats.tip.ttft')} />
        <Tile id="duration" label={t('chat.stats.duration')} value={secs(v.durationMs)} side={null} tip={t('chat.stats.tip.duration')} />
      </dl>
    </section>
  );
}
