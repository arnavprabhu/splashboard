import { useEffect, useState } from 'preact/hooks';
import { t } from '../strings/en';
import { Button } from './Button';

export interface OfflineScreenProps {
  /** Seconds until the next automatic attempt, or null while one is running. */
  retryIn: number | null;
  onRetry: () => void;
  lastSeen: string | null;
  /** Opened from the menu bar app (`?host=app`): it starts the manager itself. */
  hostedByApp?: boolean;
}

function isLoopback(host: string): boolean {
  return host === 'localhost' || host === '127.0.0.1' || host === '[::1]' || host === '::1';
}

/** Full-sheet state when the manager is unreachable (docs/ui/01 §5). */
export function OfflineScreen({ retryIn, onRetry, lastSeen, hostedByApp }: OfflineScreenProps) {
  const [starting, setStarting] = useState(Boolean(hostedByApp));
  useEffect(() => {
    if (!hostedByApp) return;
    const timer = setTimeout(() => setStarting(false), 10_000);
    return () => clearTimeout(timer);
  }, [hostedByApp]);
  const [before, after] = t('shell.offline.body').split('{cmd}');
  return (
    <section class="band" data-testid="offline-screen">
      <div class="stack" style={{ gap: '24px' }}>
        <div role="alert">
          <h1 class="display-m page-title" tabIndex={-1}>
            {starting ? t('shell.offline.starting') : t('shell.offline.title')}
          </h1>
        </div>
        <p class="body" style={{ maxWidth: '60ch' }}>
          {before}
          <code class="mono">splash start</code>
          {after}
          {!isLoopback(location.hostname) && <> {t('shell.offline.lan')}</>}
        </p>
        {!starting && (
          <div class="cluster" style={{ alignItems: 'center' }}>
            <Button onClick={onRetry}>{t('common.retry')}</Button>
            <span class="meta tnum" aria-live="polite" data-testid="offline-countdown">
              {retryIn === null ? t('shell.offline.retrying') : t('shell.offline.retry_in', { s: retryIn })}
            </span>
          </div>
        )}
        <p class="meta">
          {t('shell.offline.last_seen')} <span class="tnum">{lastSeen ?? '—'}</span> · <span class="mono">{location.origin}</span>
        </p>
      </div>
    </section>
  );
}
