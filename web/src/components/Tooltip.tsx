import type { ComponentChildren, VNode } from 'preact';
import { cloneElement } from 'preact';
import { useEffect, useId, useRef, useState } from 'preact/hooks';

/** `<kbd>` with a 1px rule, mono 12px. */
export function Kbd({ children }: { children: ComponentChildren }) {
  return <kbd class="kbd">{children}</kbd>;
}

export interface TooltipProps {
  /** Tooltip text. */
  text: ComponentChildren;
  /** One focusable element (button, link, or an element with tabIndex). */
  children: VNode<Record<string, unknown>>;
}

/**
 * A small ink block shown on hover, focus and long-press; hidden on Esc (docs/ui/00 §4.3).
 * Wires `aria-describedby` onto the child.
 */
export function Tooltip({ text, children }: TooltipProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const press = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open]);
  useEffect(() => () => clearTimeout(press.current), []);
  const prior = (children.props['aria-describedby'] as string | undefined) ?? '';
  const trigger = cloneElement(children, {
    'aria-describedby': [prior, id].filter(Boolean).join(' '),
  });
  return (
    <span
      class="tooltip-wrap"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocusIn={() => setOpen(true)}
      onFocusOut={() => setOpen(false)}
      onTouchStart={() => {
        press.current = setTimeout(() => setOpen(true), 450);
      }}
      onTouchEnd={() => clearTimeout(press.current)}
    >
      {trigger}
      <span id={id} role="tooltip" class="tooltip" data-open={String(open)}>
        {text}
      </span>
    </span>
  );
}
