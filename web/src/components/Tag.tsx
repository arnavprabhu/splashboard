import type { ComponentChildren } from 'preact';

export type TagTone = 'ink' | 'mute' | 'acc';

/**
 * Small outlined label: source chips, Restart badges, fit badges. `acc` keeps ink text with an
 * accent border (12px accent text fails AA in light).
 */
export function Tag({ children, tone = 'ink', title, dots }: { children: ComponentChildren; tone?: TagTone; title?: string; dots?: boolean }) {
  return (
    <span class={dots ? 'tag loading-dots' : 'tag'} data-tone={tone} data-accent={tone === 'acc' ? 'true' : undefined} title={title}>
      {children}
    </span>
  );
}
