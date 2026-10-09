import { signal } from '@preact/signals';
import { useEffect, useRef } from 'preact/hooks';
import { t } from '../strings/en';

/**
 * Toasts: a solid ink block, bottom-start, one at a time,
 * 6 s (errors stay until dismissed), hover/focus pauses the timer, Esc dismisses. Never accent.
 */

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastItem {
  id: number;
  message: string;
  /** Verbatim API/engine detail shown in mono after the headline. */
  detail?: string;
  tone: 'info' | 'error';
  action?: ToastAction;
}

export const TOAST_MS = 6000;

export const toastQueue = signal<ToastItem[]>([]);
let nextId = 1;

export interface ToastOptions {
  tone?: 'info' | 'error';
  detail?: string;
  action?: ToastAction;
}

export function toast(message: string, options: ToastOptions = {}): number {
  const item: ToastItem = { id: nextId++, message, tone: options.tone ?? 'info' };
  if (options.detail) item.detail = options.detail;
  if (options.action) item.action = options.action;
  toastQueue.value = [...toastQueue.value, item];
  return item.id;
}

/** Error toast: "Couldn't stop the engine." + the API message in mono. */
export function toastError(headline: string, err?: unknown): number {
  const detail = err === undefined ? undefined : describeError(err);
  return toast(headline, detail ? { tone: 'error', detail } : { tone: 'error' });
}

export function describeError(err: unknown): string {
  if (err && typeof err === 'object') {
    const e = err as { status?: number; code?: string | null; message?: string };
    const prefix = [e.status && e.status > 0 ? String(e.status) : '', e.code ?? ''].filter(Boolean).join(' ');
    return [prefix, e.message ?? ''].filter(Boolean).join(' · ');
  }
  return String(err);
}

export function dismissToast(id?: number): void {
  const [head, ...rest] = toastQueue.value;
  if (!head) return;
  if (id === undefined || head.id === id) toastQueue.value = rest;
  else toastQueue.value = toastQueue.value.filter((x) => x.id !== id);
}

/** Mounted once in the shell. */
export function ToastHost() {
  const current = toastQueue.value[0];
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const paused = useRef(false);

  const arm = () => {
    clearTimeout(timer.current);
    if (!current || current.tone === 'error' || paused.current) return;
    timer.current = setTimeout(() => dismissToast(current.id), TOAST_MS);
  };

  useEffect(() => {
    arm();
    return () => clearTimeout(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.id]);

  useEffect(() => {
    if (!current) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') dismissToast(current.id);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [current?.id]);

  if (!current) return <div class="toast-host" aria-live="polite" />;
  return (
    <div class="toast-host">
      <div
        class="toast"
        role={current.tone === 'error' ? 'alert' : 'status'}
        data-tone={current.tone}
        data-testid="toast"
        onMouseEnter={() => {
          paused.current = true;
          clearTimeout(timer.current);
        }}
        onMouseLeave={() => {
          paused.current = false;
          arm();
        }}
        onFocusIn={() => {
          paused.current = true;
          clearTimeout(timer.current);
        }}
        onFocusOut={() => {
          paused.current = false;
          arm();
        }}
      >
        <span class="toast-msg">
          {current.message}
          {current.detail && <code class="toast-detail mono">{current.detail}</code>}
        </span>
        {current.action && (
          <button
            type="button"
            class="toast-btn"
            onClick={() => {
              current.action?.onClick();
              dismissToast(current.id);
            }}
          >
            {current.action.label}
          </button>
        )}
        <button type="button" class="toast-btn" aria-label={t('common.close')} onClick={() => dismissToast(current.id)}>
          ×
        </button>
      </div>
    </div>
  );
}
