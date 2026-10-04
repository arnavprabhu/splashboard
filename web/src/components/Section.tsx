import type { ComponentChildren } from 'preact';

export interface SectionProps {
  /** Section label (label type) shown in the left column. */
  label?: ComponentChildren;
  /** Optional meta line under the label. */
  meta?: ComponentChildren;
  id?: string;
  tight?: boolean;
  children: ComponentChildren;
}

/** A band with a 2px top rule; with a label it uses the label + content row. */
export function Section({ label, meta, id, tight, children }: SectionProps) {
  const cls = tight ? 'band tight' : 'band';
  if (label === undefined) {
    return (
      <section class={cls} id={id}>
        {children}
      </section>
    );
  }
  return (
    <section class={cls} id={id} aria-labelledby={id ? `${id}-label` : undefined}>
      <LabelRow label={label} meta={meta} labelId={id ? `${id}-label` : undefined}>
        {children}
      </LabelRow>
    </section>
  );
}

export interface LabelRowProps {
  label: ComponentChildren;
  meta?: ComponentChildren;
  labelId?: string | undefined;
  children: ComponentChildren;
}

/** Label at flex 1 1 200px, content at flex 3 1 560px; stacks on narrow screens. */
export function LabelRow({ label, meta, labelId, children }: LabelRowProps) {
  return (
    <div class="label-row">
      <div class="label-row-label">
        <h2 class="label" id={labelId}>
          {label}
        </h2>
        {meta && <p class="meta">{meta}</p>}
      </div>
      <div class="label-row-content">{children}</div>
    </div>
  );
}

export interface PageHeaderProps {
  title: string;
  meta?: ComponentChildren;
  actions?: ComponentChildren;
  size?: 'l' | 'm';
}

/** Page title in display type, ending in a period for the poster feel. */
export function PageHeader({ title, meta, actions, size = 'l' }: PageHeaderProps) {
  return (
    <header class="page-head stack">
      <h1 class={`page-title display-${size}`}>{title}</h1>
      {(meta || actions) && (
        <div class="cluster" style={{ justifyContent: 'space-between' }}>
          {meta && <div class="meta">{meta}</div>}
          {actions && <div class="cluster">{actions}</div>}
        </div>
      )}
    </header>
  );
}
