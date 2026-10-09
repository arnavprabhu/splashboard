import type { ComponentChildren } from 'preact';
import { useEffect, useId, useRef, useState } from 'preact/hooks';

export interface MenuItem {
  key: string;
  label: ComponentChildren;
  /** Plain text for type-ahead; defaults to the label when it is a string. */
  text?: string;
  /** Second line in meta type (e.g. the reason an item is disabled). */
  detail?: ComponentChildren;
  checked?: boolean;
  disabled?: boolean;
  onSelect?: () => void;
}

export interface MenuGroup {
  label?: string;
  items: readonly MenuItem[];
}

export interface MenuProps {
  /** Trigger label; "▾" is appended. */
  label: ComponentChildren;
  /** Accessible name when the label is not descriptive. */
  ariaLabel?: string;
  items?: readonly MenuItem[];
  groups?: readonly MenuGroup[];
  variant?: 'outline' | 'solid' | 'accent' | 'text';
  size?: 'm' | 's';
  disabled?: boolean;
  align?: 'start' | 'end';
  /** Radio semantics (one checked item) vs plain actions. */
  radio?: boolean;
  testId?: string;
}

/**
 * Square panel opened by a button ending in ▾. ↓/↑, Home/End, type-ahead,
 * Esc closes and refocuses the trigger, Enter selects. Hover/focus fills the accent.
 */
export function Menu({ label, ariaLabel, items, groups, variant = 'outline', size = 'm', disabled, align = 'start', radio, testId }: MenuProps) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const typed = useRef({ text: '', at: 0 });
  const allGroups: readonly MenuGroup[] = groups ?? [{ items: items ?? [] }];

  const focusables = () => Array.from(panel.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]:not([aria-disabled="true"])') ?? []);

  useEffect(() => {
    if (!open) return;
    const first = panel.current?.querySelector<HTMLElement>('[aria-checked="true"]:not([aria-disabled="true"])') ?? focusables()[0];
    first?.focus();
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!panel.current?.contains(target) && !trigger.current?.contains(target)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const close = (refocus = true) => {
    setOpen(false);
    if (refocus) trigger.current?.focus();
  };

  const onKeyDown = (e: KeyboardEvent) => {
    const list = focusables();
    const i = list.indexOf(document.activeElement as HTMLElement);
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      list[(i + 1) % list.length]?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      list[(i - 1 + list.length) % list.length]?.focus();
    } else if (e.key === 'Home') {
      e.preventDefault();
      list[0]?.focus();
    } else if (e.key === 'End') {
      e.preventDefault();
      list[list.length - 1]?.focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === 'Tab') {
      close(false);
    } else if (e.key.length === 1 && /\S/.test(e.key)) {
      const now = Date.now();
      typed.current = { text: (now - typed.current.at < 700 ? typed.current.text : '') + e.key.toLowerCase(), at: now };
      const match = list.find((el) => (el.textContent ?? '').trim().toLowerCase().startsWith(typed.current.text));
      match?.focus();
    }
  };

  return (
    <span class="menu" data-align={align}>
      <button
        ref={trigger}
        type="button"
        class="btn"
        data-variant={variant}
        data-size={size}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-label={ariaLabel}
        disabled={disabled}
        data-testid={testId}
        onClick={() => setOpen(!open)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        {label} <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div ref={panel} id={id} class="menu-panel" role="menu" onKeyDown={onKeyDown}>
          {allGroups.map((g, gi) => (
            <div key={g.label ?? gi} role="group" aria-label={g.label} class="menu-group">
              {g.label && <div class="menu-group-label meta">{g.label}</div>}
              {g.items.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  class="menu-item nav"
                  role={radio ? 'menuitemradio' : 'menuitem'}
                  aria-checked={radio ? Boolean(item.checked) : undefined}
                  aria-disabled={item.disabled ? 'true' : undefined}
                  tabIndex={-1}
                  onClick={() => {
                    if (item.disabled) return;
                    close();
                    item.onSelect?.();
                  }}
                >
                  <span class="menu-item-label">
                    <span class="menu-check" aria-hidden="true">
                      {item.checked ? '●' : ''}
                    </span>
                    {item.label}
                  </span>
                  {item.detail && <span class="menu-item-detail meta">{item.detail}</span>}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </span>
  );
}
