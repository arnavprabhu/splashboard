import type { ComponentChildren } from 'preact';
import { useId, useLayoutEffect, useRef } from 'preact/hooks';
import { t } from '../strings/en';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface SheetProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ComponentChildren;
  footer?: ComponentChildren;
  /** While busy, Esc and the backdrop do not close the sheet. */
  busy?: boolean | undefined;
  /** `alertdialog` for confirmations. */
  role?: 'dialog' | 'alertdialog';
  /** Width: 640px (default) or 720px for the model drawer. */
  width?: 'm' | 'l';
  /** Extra content in the head, left of CLOSE (e.g. a COPY ID button). */
  headActions?: ComponentChildren;
  testId?: string;
}

/** Full-height sheet from the right: 2px left rule, page dimmed with --bg at 80%, focus trapped, Esc closes. */
export function Sheet({ open, title, onClose, children, footer, busy, role = 'dialog', width = 'm', headActions, testId }: SheetProps) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const busyRef = useRef(busy);
  busyRef.current = busy;

  // Layout effect: the Esc/Tab handler must exist as soon as the sheet is on screen.
  useLayoutEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const node = panel.current;
    // Focus moves to the title; Tab then reaches the controls.
    node?.querySelector<HTMLElement>('.sheet-title')?.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        if (!busyRef.current) closeRef.current();
        return;
      }
      if (e.key !== 'Tab' || !node) return;
      const items = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) {
        e.preventDefault();
        node.focus();
        return;
      }
      const head = items[0]!;
      const tail = items[items.length - 1]!;
      const active = document.activeElement;
      if (e.shiftKey && (active === head || active === node)) {
        e.preventDefault();
        tail.focus();
      } else if (!e.shiftKey && active === tail) {
        e.preventDefault();
        head.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      document.body.style.overflow = overflow;
      previous?.focus?.();
    };
  }, [open]);

  if (!open) return null;
  return (
    <div
      class="sheet-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div class="sheet" data-width={width} role={role} aria-modal="true" aria-labelledby={titleId} ref={panel} tabIndex={-1} data-testid={testId}>
        <div class="sheet-head">
          <h2 class="heading sheet-title" id={titleId} tabIndex={-1}>
            {title}
          </h2>
          <div class="cluster" style={{ gap: '16px', flexWrap: 'nowrap' }}>
            {headActions}
            <button type="button" class="btn" data-variant="text" onClick={onClose} disabled={busy}>
              {t('common.close')}
            </button>
          </div>
        </div>
        <div class="sheet-body">{children}</div>
        {footer && <div class="sheet-foot">{footer}</div>}
      </div>
    </div>
  );
}
