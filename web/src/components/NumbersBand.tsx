import type { ComponentChildren } from 'preact';

export interface StatProps {
  label: string;
  value: ComponentChildren;
  unit?: string;
  sub?: ComponentChildren;
  /** Accent numeral: only for values that need attention (e.g. failures > 0). */
  accent?: boolean;
  title?: string;
}

export function Stat({ label, value, unit, sub, accent, title }: StatProps) {
  return (
    <div class="stat" title={title}>
      <div class="label">{label}</div>
      <span class="stat-value display-number" data-accent={String(Boolean(accent))}>
        {value}
        {unit && <span class="stat-unit">{unit}</span>}
      </span>
      {sub && <div class="stat-sub meta">{sub}</div>}
    </div>
  );
}

export interface NumbersBandProps {
  label?: string;
  children: ComponentChildren;
  actions?: ComponentChildren;
}

/** 2px top rule, auto-fit grid of stats separated by 1px rules. Replaces oMLX stat cards. */
export function NumbersBand({ label, children, actions }: NumbersBandProps) {
  return (
    <section class="band" aria-label={label}>
      {(label || actions) && (
        <div class="cluster" style={{ justifyContent: 'space-between', marginBottom: '20px' }}>
          {label && <h2 class="label">{label}</h2>}
          {actions && <div class="cluster">{actions}</div>}
        </div>
      )}
      <div class="numbers-clip">
        <div class="numbers-grid numbers">{children}</div>
      </div>
    </section>
  );
}
