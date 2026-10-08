import type { ComponentChildren } from 'preact';
import { useRef } from 'preact/hooks';
import { useReveal } from '../lib/reveal';

/**
 * A block that eases in when it enters the viewport (SPEC §18.4, DESIGN.md Motion). Used only on
 * the long pages (history and the benchmark compare view). Not exported from `components/index.ts`,
 * so the shell's initial bundle does not grow.
 */
export function RevealBlock({ children }: { children: ComponentChildren }) {
  const ref = useRef<HTMLDivElement>(null);
  useReveal(ref);
  return <div ref={ref}>{children}</div>;
}
