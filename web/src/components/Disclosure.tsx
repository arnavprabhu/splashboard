import type { ComponentChildren } from 'preact';

export interface DisclosureProps {
  summary: ComponentChildren;
  children: ComponentChildren;
  open?: boolean;
  summaryClass?: string;
}

/** Native details/summary with the ▸ glyph rotating when open. */
export function Disclosure({ summary, children, open, summaryClass = 'label' }: DisclosureProps) {
  return (
    <details class="disclosure" open={open}>
      <summary class={summaryClass}>
        <span class="disclosure-glyph" aria-hidden="true">
          ▸
        </span>
        {summary}
      </summary>
      <div class="disclosure-body">{children}</div>
    </details>
  );
}
