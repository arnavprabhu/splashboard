import { useState } from 'preact/hooks';
import type { Alert, AlertAction } from '../api/types';
import { canDismiss, sortAlerts } from '../store';
import { t } from '../strings/en';

export interface AlertBandProps {
  alerts: readonly Alert[];
  onAction?: (alert: Alert, action: AlertAction) => void;
  onDismiss?: (alert: Alert) => void;
  /** Action ids currently running (button in loading state). */
  pending?: ReadonlySet<string>;
}

/** Items shown before "+N more ▾" (decision S4). */
export const ALERTS_VISIBLE = 3;

function hhmm(iso: string | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

/** Full-width accent band under the nav; most severe first (SPEC §10.1, §16.3, docs/ui/01 §4). */
export function AlertBand({ alerts, onAction, onDismiss, pending }: AlertBandProps) {
  const [expanded, setExpanded] = useState(false);
  if (alerts.length === 0) return null;
  const sorted = sortAlerts(alerts);
  const shown = expanded ? sorted : sorted.slice(0, ALERTS_VISIBLE);
  const hidden = sorted.length - shown.length;
  return (
    <section class="alertband" aria-label="Alerts" aria-live="polite" data-testid="alertband">
      {shown.map((alert) => {
        const last = hhmm(alert.updated_at ?? alert.raised_at);
        const first = hhmm(alert.raised_at);
        return (
          <div class="alertband-item" key={alert.id} data-severity={alert.severity} role={alert.severity === 'critical' ? 'alert' : undefined}>
            <span class="label">{t(`alert.${alert.severity}`)}</span>
            <span class="alertband-msg">
              {alert.title && alert.title !== alert.message ? (
                <>
                  <strong>{alert.title}</strong> {alert.message}
                </>
              ) : (
                alert.message
              )}
              {alert.count !== undefined && alert.count > 1 && (
                <span class="tnum alertband-recur" title={first ? t('alert.recur_help', { time: first }) : undefined}>
                  {' '}
                  {t('alert.recur', { n: alert.count, time: last ?? '—' })}
                </span>
              )}
            </span>
            {alert.actions.map((action) => {
              const busy = pending?.has(`${alert.id}:${action.id}`);
              return (
                <button
                  key={action.id}
                  type="button"
                  class="btn"
                  data-size="s"
                  aria-busy={busy ? 'true' : undefined}
                  disabled={busy}
                  onClick={() => onAction?.(alert, action)}
                >
                  <span class={busy ? 'loading-dots' : undefined}>{action.label}</span>
                </button>
              );
            })}
            {onDismiss && canDismiss(alert) && (
              <button
                type="button"
                class="btn"
                data-variant="text"
                data-size="s"
                aria-label={t('alert.dismiss_label', { message: alert.title ?? alert.message })}
                onClick={() => onDismiss(alert)}
              >
                {t('alert.dismiss')}
              </button>
            )}
          </div>
        );
      })}
      {(hidden > 0 || expanded) && sorted.length > ALERTS_VISIBLE && (
        <div class="alertband-item">
          <button type="button" class="btn" data-variant="text" data-size="s" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
            {expanded ? t('alert.fewer') : t('alert.more', { n: hidden })}
          </button>
        </div>
      )}
    </section>
  );
}
