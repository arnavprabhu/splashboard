import { render } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import { ChatStats, contextUsed } from '../src/routes/chat/ChatStats';
import type { ChatMessage } from '../src/routes/chat/types';

const msg = (role: string, content: string, meta: Record<string, unknown> = {}): ChatMessage =>
  ({ id: Math.random().toString(36), parent: null, role, content, created_at: '', meta }) as unknown as ChatMessage;

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

  it('shows the percentage, with the totals in the tooltip, plus PP, TG and TTFT', () => {
    const thread = [
      msg('assistant', 'yo', {
        usage: { prompt_tokens: 1000, completion_tokens: 24 },
        timings: { prompt_per_second: 480, predicted_per_second: 42 },
        ttft_ms: 800,
      }),
    ];
    const { container } = render(<ChatStats thread={thread} busy={false} progressTotal={null} context={4096} contextEstimated={false} live={null} />);
    const spans = [...container.querySelectorAll('span')];
    expect(spans.map((s) => s.textContent)).toEqual(['CTX 25%', 'PP 480 tok/s', 'TG 42.0 tok/s', 'TTFT 800 ms']);
    expect(spans[0]!.getAttribute('title')).toBe('Context used: 1,024 of 4,096 tokens');
  });
});
