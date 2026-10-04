import type { ComponentChildren } from 'preact';

/** Small outlined label: source chips, Restart badges, fit badges. */
export function Tag({ children, tone = 'ink', title }: { children: ComponentChildren; tone?: 'ink' | 'mute'; title?: string }) {
  return (
    <span class="tag" data-tone={tone} title={title}>
      {children}
    </span>
  );
}
