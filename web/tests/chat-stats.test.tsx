import { render, waitFor } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import type { LiveMetrics } from '../src/api/models';
import { ContextMeter, StatTiles, contextUsed, tileValues } from '../src/routes/chat/ChatStats';
import type { ChatMessage } from '../src/routes/chat/types';

const msg = (role: string, content: string, meta: Record<string, unknown> = {}): ChatMessage =>
  ({ id: Math.random().toString(36), parent: null, role, content, created_at: '', meta }) as unknown as ChatMessage;

const DONE = msg('assistant', 'yo', {
  usage: { prompt_tokens: 1000, completion_tokens: 24, prompt_tokens_details: { cached_tokens: 512 } },
  timings: { prompt_per_second: 480, predicted_per_second: 42, prompt_ms: 2000, predicted_ms: 600 },
  ttft_ms: 800,
  duration_ms: 3150,
});

describe('chat stats', () => {
  it('counts the last reply’s prompt and output as context used', () => {
    const thread = [msg('user', 'hi'), msg('assistant', 'yo', { usage: { prompt_tokens: 1000, completion_tokens: 234 } })];
    expect(contextUsed(thread, false, null)).toBe(1234);
  });

  it('adds the streaming output to the prompt the engine reports', () => {
    const thread = [msg('user', 'hi'), msg('assistant', 'x'.repeat(400))];
    expect(contextUsed(thread, true, 5000)).toBe(5100);
    expect(contextUsed(thread, true, null)).toBe(0);
  });

  it('shows tokens and the percentage, and the totals on hover and focus', async () => {
    const { container, getByTestId } = render(<ContextMeter thread={[DONE]} busy={false} promptTotal={null} context={4096} contextEstimated={false} />);
    const meter = getByTestId('chat-context');
    expect(meter.textContent).toBe('1,024 tokens · 25%');
    expect(meter.tabIndex).toBe(0);
    const tip = container.querySelector('[role="tooltip"]')!;
    expect(tip.textContent).toBe('Context used: 1,024 of 4,096 tokens');
    expect(meter.getAttribute('aria-describedby')).toContain(tip.id);
    expect(tip.getAttribute('data-open')).toBe('false');
    meter.focus();
    await waitFor(() => expect(container.querySelector('[role="tooltip"]')!.getAttribute('data-open')).toBe('true'));
  });

  it('shows only the token count when the window is unknown', () => {
    const { getByTestId } = render(<ContextMeter thread={[DONE]} busy={false} promptTotal={null} context={null} contextEstimated={false} />);
    expect(getByTestId('chat-context').textContent).toBe('1,024 tokens');
  });

  it('tiles show the last reply’s prefill, token gen, TTFT and duration', () => {
    const { getByTestId } = render(<StatTiles thread={[msg('user', 'hi'), DONE]} busy={false} live={null} promptTotal={null} startedAt={null} />);
    expect(getByTestId('chat-tile-prefill').textContent).toBe('Prefill (t/s)4801,000 tok');
    expect(getByTestId('chat-tile-decode').textContent).toBe('Token gen (t/s)42.024 tok');
    expect(getByTestId('chat-tile-ttft').textContent).toBe('TTFT (s)0.80512 cached');
    expect(getByTestId('chat-tile-duration').textContent).toBe('Duration (s)3.15');
  });

  it('tiles show a dash when nothing is known', () => {
    const { getByTestId } = render(<StatTiles thread={[]} busy={false} live={null} promptTotal={null} startedAt={null} />);
    expect(getByTestId('chat-tile-prefill').textContent).toBe('Prefill (t/s)—');
    expect(getByTestId('chat-tile-duration').textContent).toBe('Duration (s)—');
  });

  it('while a reply runs, rates come from the engine’s live sample', () => {
    const live = { t: 1, throughput: { prefill_tps: 1234.5, decode_tps: 61.25 } } as unknown as LiveMetrics;
    const thread = [msg('user', 'hi'), DONE, msg('user', 'more'), msg('assistant', 'x'.repeat(40), { ttft_ms: 250 })];
    const v = tileValues(thread, true, live, 2048, 10_000, 12_500);
    expect(v).toEqual({ prefill: 1234.5, promptTokens: 2048, decode: 61.25, outTokens: 10, ttftMs: 250, cached: null, durationMs: 2500 });
  });

  it('falls back to the engine’s timings for the duration of older replies', () => {
    const old = msg('assistant', 'yo', { timings: { prompt_ms: 100, predicted_ms: 900 } });
    expect(tileValues([old], false, null, null, null, 0).durationMs).toBe(1000);
  });
});
