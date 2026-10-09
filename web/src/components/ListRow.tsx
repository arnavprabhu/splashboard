import type { ComponentChildren } from 'preact';
import { Link } from 'wouter-preact';

export interface ListRowProps {
  name: string;
  /** Right-aligned label, e.g. "01 — GGUF · 21 GB". */
  label?: ComponentChildren;
  href?: string;
  onSelect?: () => void;
  /** Revealed on hover/focus; always visible on touch and when `expanded`. */
  actions?: ComponentChildren;
  /** Extra line under the name, e.g. a status chip or progress. */
  detail?: ComponentChildren;
  expanded?: boolean;
  nameClass?: string;
}

/** Display-type name left, label right, separated by 1px rules. Hover/focus fills the accent. */
export function ListRow({ name, label, href, onSelect, actions, detail, expanded, nameClass = 'display-m' }: ListRowProps) {
  const interactive = Boolean(href || onSelect || actions);
  const nameCls = `listrow-name ${nameClass}`;
  const nameEl = href ? (
    <Link href={href} class={nameCls}>
      {name}
    </Link>
  ) : onSelect ? (
    <button type="button" class={nameCls} onClick={onSelect}>
      {name}
    </button>
  ) : (
    <span class={nameCls}>{name}</span>
  );
  return (
    <li class="listrow" data-interactive={String(interactive)} data-expanded={String(Boolean(expanded))}>
      <div class="listrow-main">
        {nameEl}
        {label && <span class="listrow-label label">{label}</span>}
      </div>
      {detail && <div class="meta">{detail}</div>}
      {actions && (
        <div class="listrow-extra">
          <div>
            <div class="listrow-actions">{actions}</div>
          </div>
        </div>
      )}
    </li>
  );
}

export function List({ children, label }: { children: ComponentChildren; label?: string }) {
  return (
    <ul class="list" aria-label={label}>
      {children}
    </ul>
  );
}
