import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ComponentChildren } from 'preact';

export type ButtonVariant = 'outline' | 'text' | 'solid' | 'accent';

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'size'> {
  variant?: ButtonVariant;
  size?: 'm' | 's';
  /** Busy: label gets animated dots, aria-busy, disabled. */
  loading?: boolean | undefined;
  children: ComponentChildren;
}

/** Square text button. `accent` is for the one important action on screen. */
export function Button({ variant = 'outline', size = 'm', type = 'button', class: cls, loading, disabled, children, ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      class={['btn', cls].filter(Boolean).join(' ')}
      data-variant={variant}
      data-size={size}
      aria-busy={loading ? 'true' : undefined}
      disabled={Boolean(disabled) || Boolean(loading)}
      {...rest}
    >
      {loading ? <span class="loading-dots">{children}</span> : children}
    </button>
  );
}

export interface ExternalLinkProps extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href' | 'children' | 'role'> {
  class?: string;
  href: string;
  children: ComponentChildren;
}

/** External link with the accent ↗ glyph (DESIGN.md "Large link"). */
export function ExternalLink({ href, children, class: cls, ...rest }: ExternalLinkProps) {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" class={cls} {...rest}>
      {children}
      <span class="acc" aria-hidden="true">
        {' ↗'}
      </span>
      <span class="visually-hidden"> (opens in a new tab)</span>
    </a>
  );
}
