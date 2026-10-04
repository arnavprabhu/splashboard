import type { ComponentChildren } from 'preact';
import { t } from '../strings/en';
import { Banner } from './Banner';

export interface EmptyProps {
  title: string;
  children?: ComponentChildren;
  action?: ComponentChildren;
  /** `l`: whole-page empty with the statement in lead type (docs/ui/00 §7.2). */
  size?: 'm' | 'l';
}

export function Empty({ title, children, action, size = 'm' }: EmptyProps) {
  return (
    <div class="empty" data-size={size}>
      <p class={size === 'l' ? 'lead' : 'label'}>{title}</p>
      {children && <p class="body mute">{children}</p>}
      {action && <div class="cluster">{action}</div>}
    </div>
  );
}

export function Loading({ label = t('common.loading') }: { label?: string }) {
  return (
    <p class="meta loading-dots" role="status" aria-live="polite">
      {label}
    </p>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div class="empty" role="alert">
      <p class="label">{t('error.generic')}</p>
      <p class="body">{message}</p>
      {onRetry && (
        <div class="cluster">
          <button type="button" class="btn" onClick={onRetry}>
            {t('common.retry')}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * A band's own failed fetch (docs/ui/00 §7.3): "Couldn't load <thing>." · the API message in
 * mono · Retry. Other bands keep working.
 */
export function LoadError({ thing, error, onRetry }: { thing: string; error: unknown; onRetry?: () => void }) {
  const e = error as { status?: number; code?: string | null; message?: string } | null;
  const notBuilt = e?.status === 501;
  const detail = e && typeof e === 'object' ? [e.status ? String(e.status) : '', e.code ?? '', e.message ?? ''].filter(Boolean).join(' · ') : String(error);
  return (
    <Banner
      tone="warn"
      title={t('common.couldnt_load', { thing })}
      actions={
        onRetry && (
          <button type="button" class="btn" data-size="s" onClick={onRetry}>
            {t('common.retry')}
          </button>
        )
      }
    >
      {notBuilt && <span>{t('common.not_implemented')} </span>}
      <code class="mono">{detail}</code>
    </Banner>
  );
}
