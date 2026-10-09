import type { ComponentChildren } from 'preact';

export interface RowProps {
  name: string;
  /** `display-m` or `heading` (Manager, Search, Supported). */
  nameClass?: string;
  /** Opens the detail drawer; Enter on the name does the same. */
  onOpen?: () => void;
  /** Accessible name of the name button ("Details for <id>"). */
  openLabel?: string;
  /** Left of the name: the select-mode checkbox. */
  leading?: ComponentChildren;
  /** Chip or tags right of the name, before the label. */
  status?: ComponentChildren;
  /** `NN — FORMAT · SIZE`. */
  label?: ComponentChildren;
  /** The meta line (always visible). */
  detail?: ComponentChildren;
  /** Always visible below the meta line: progress bar, inline banner. */
  body?: ComponentChildren;
  /** Revealed on hover/focus; always visible on touch, when expanded, and on the pinned row. */
  actions?: ComponentChildren;
  expanded?: boolean;
  /** The active model: no hover fill, actions always shown. */
  pinned?: boolean;
  testId?: string;
  modelId?: string;
}

/**
 * A model list row built on the shared `.listrow` styles (components.css), with the extra
 * slots the Models pages need (checkbox, status, body). Keyboard: see `onListKeyDown`.
 */
export function Row({ name, nameClass = 'display-m', onOpen, openLabel, leading, status, label, detail, body, actions, expanded, pinned, testId, modelId }: RowProps) {
  const interactive = !pinned && Boolean(actions || onOpen);
  const cls = `listrow-name ${nameClass}`;
  return (
    <li
      class="listrow mrow"
      data-interactive={String(interactive)}
      data-expanded={String(Boolean(expanded || pinned))}
      data-pinned={pinned ? 'true' : undefined}
      data-testid={testId}
      data-model={modelId}
    >
      <div class="listrow-main">
        {leading && <span class="mrow-lead">{leading}</span>}
        {onOpen ? (
          <button type="button" class={cls} onClick={onOpen} aria-label={openLabel} data-row-name="true">
            {name}
          </button>
        ) : (
          <span class={cls} tabIndex={-1} data-row-name="true">
            {name}
          </span>
        )}
        {(status || label) && (
          <span class="mrow-right">
            {status}
            {label && <span class="listrow-label label tnum">{label}</span>}
          </span>
        )}
      </div>
      {detail && <div class="meta mrow-detail">{detail}</div>}
      {body && <div class="mrow-body">{body}</div>}
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

/** ↓/↑ move between row names, → focuses the row's first action. */
export function onListKeyDown(e: KeyboardEvent): void {
  const target = e.target as HTMLElement | null;
  if (!target?.matches('[data-row-name="true"]')) return;
  const list = target.closest('ul');
  const row = target.closest('li');
  if (!list || !row) return;
  const names = Array.from(list.querySelectorAll<HTMLElement>(':scope > li [data-row-name="true"]'));
  const i = names.indexOf(target);
  if (e.key === 'ArrowDown' && i < names.length - 1) {
    e.preventDefault();
    names[i + 1]!.focus();
  } else if (e.key === 'ArrowUp' && i > 0) {
    e.preventDefault();
    names[i - 1]!.focus();
  } else if (e.key === 'ArrowRight') {
    const first = row.querySelector<HTMLElement>('.listrow-actions button, .listrow-actions a');
    if (first) {
      e.preventDefault();
      first.focus();
    }
  }
}

export function RowList({ label, children, testId }: { label: string; children: ComponentChildren; testId?: string }) {
  return (
    <ul class="list mlist" aria-label={label} onKeyDown={onListKeyDown} data-testid={testId}>
      {children}
    </ul>
  );
}
