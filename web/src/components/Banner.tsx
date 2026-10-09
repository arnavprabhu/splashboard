import type { ComponentChildren } from 'preact';

export interface BannerProps {
  tone?: 'info' | 'warn' | 'critical';
  title?: string;
  children: ComponentChildren;
  actions?: ComponentChildren;
}

/** Inline, in-page notice. Page-wide health alerts use AlertBand instead. */
export function Banner({ tone = 'info', title, children, actions }: BannerProps) {
  return (
    <div class="banner" data-tone={tone} role={tone === 'critical' ? 'alert' : 'note'}>
      {title && <span class="label">{title}</span>}
      <div class="banner-msg">{children}</div>
      {actions}
    </div>
  );
}
