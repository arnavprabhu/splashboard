import type { LiveMetrics } from '../../api/models';
import { formatMs, formatTokPerSec } from '../../lib/format';
import { t } from '../../strings/chat';
import { estimateTokens } from './accumulator';
import { metaOf, type ChatMessage } from './types';

export interface ChatStatsProps {
  /** The active branch, oldest first (the streaming reply is the last assistant message). */
  thread: ChatMessage[];
  busy: boolean;
  /** Prompt tokens the engine reports while it reads the prompt. */
  progressTotal: number | null;
  context: number | null;
  contextEstimated: boolean;
  /** The engine's own live rates, used while a reply is running. */
  live: LiveMetrics | null;
}

/** Tokens the conversation takes up now: the last reply's prompt + output, plus what is streaming. */
export function contextUsed(thread: ChatMessage[], busy: boolean, progressTotal: number | null): number {
  let used = 0;
  let streaming: ChatMessage | undefined;
  for (let i = thread.length - 1; i >= 0; i--) {
    const m = thread[i]!;
    if (m.role !== 'assistant') continue;
    const u = metaOf(m).usage;
    if (u?.prompt_tokens != null) {
      used = u.prompt_tokens + (u.completion_tokens ?? 0);
      break;
    }
    if (i === thread.length - 1 && busy) streaming = m;
  }
  if (busy && streaming) {
    const out = estimateTokens(`${streaming.reasoning ?? ''}${typeof streaming.content === 'string' ? streaming.content : ''}`);
    used = Math.max(used, progressTotal ?? 0) + (progressTotal ? out : 0);
  }
  return used;
}

function Item({ label, value, tip }: { label: string; value: string; tip: string }) {
  return (
    <span title={tip} class="tnum">
      {label} {value}
    </span>
  );
}

export function ChatStats(p: ChatStatsProps) {
  const last = [...p.thread].reverse().find((m) => m.role === 'assistant');
  const meta = last ? metaOf(last) : null;
  const rate = (liveValue: number | null | undefined, doneValue: number | null | undefined) =>
    p.busy ? (liveValue ?? null) : (doneValue ?? null);
  const pp = rate(p.live?.throughput?.prefill_tps, meta?.timings?.prompt_per_second);
  const tg = rate(p.live?.throughput?.decode_tps, meta?.timings?.predicted_per_second);
  const ttft = meta?.ttft_ms ?? null;
  const used = contextUsed(p.thread, p.busy, p.progressTotal);
  const pct = p.context ? Math.min(100, (used / p.context) * 100) : 0;
  return (
    <>
      {p.context ? (
        <Item
          label={t('chat.stats.ctx')}
          value={`${pct >= 10 ? Math.round(pct) : Number(pct.toFixed(1))}%`}
          tip={t(p.contextEstimated ? 'chat.stats.tip.ctx_est' : 'chat.stats.tip.ctx', { used: used.toLocaleString('en-US'), total: p.context.toLocaleString('en-US') })}
        />
      ) : null}
      <Item label={t('chat.stats.pp')} value={formatTokPerSec(pp)} tip={t('chat.stats.tip.pp')} />
      <Item label={t('chat.stats.tg')} value={formatTokPerSec(tg)} tip={t('chat.stats.tip.tg')} />
      <Item label={t('chat.stats.ttft')} value={formatMs(ttft)} tip={t('chat.stats.tip.ttft')} />
    </>
  );
}
