import type { ComponentChildren } from 'preact';

export function Empty({ title, children, action }: { title: string; children?: ComponentChildren; action?: ComponentChildren }) {
  return (
    <div class="empty">
      <p class="label">{title}</p>
      {children && <p class="body mute">{children}</p>}
      {action && <div class="cluster">{action}</div>}
    </div>
  );
}

export function Loading({ label = 'Loading' }: { label?: string }) {
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
      <p class="label">Something went wrong</p>
      <p class="body">{message}</p>
      {onRetry && (
        <div class="cluster">
          <button type="button" class="btn" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}
    </div>
  );
}
