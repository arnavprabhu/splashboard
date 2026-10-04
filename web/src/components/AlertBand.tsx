import type { Alert, AlertAction } from '../api/types';
import { canDismiss, sortAlerts } from '../store';

export interface AlertBandProps {
  alerts: readonly Alert[];
  onAction?: (alert: Alert, action: AlertAction) => void;
  onDismiss?: (alert: Alert) => void;
}

const SEVERITY_LABEL = { critical: 'Critical', warn: 'Warning', info: 'Notice' } as const;

/** Full-width accent band under the nav; most severe first (SPEC §10.1, §16.3). */
export function AlertBand({ alerts, onAction, onDismiss }: AlertBandProps) {
  if (alerts.length === 0) return null;
  return (
    <section class="alertband" aria-label="Alerts" aria-live="polite">
      {sortAlerts(alerts).map((alert) => (
        <div
          class="alertband-item"
          key={alert.id}
          data-severity={alert.severity}
          role={alert.severity === 'critical' ? 'alert' : undefined}
        >
          <span class="label">{SEVERITY_LABEL[alert.severity]}</span>
          <span class="alertband-msg">
            {alert.title && alert.title !== alert.message ? (
              <>
                <strong>{alert.title}</strong> {alert.message}
              </>
            ) : (
              alert.message
            )}
            {alert.count !== undefined && alert.count > 1 && <span class="tnum"> ×{alert.count}</span>}
          </span>
          {alert.actions.map((action) => (
            <button key={action.id} type="button" class="btn" data-size="s" onClick={() => onAction?.(alert, action)}>
              {action.label}
            </button>
          ))}
          {onDismiss && canDismiss(alert) && (
            <button
              type="button"
              class="btn"
              data-variant="text"
              data-size="s"
              aria-label={`Dismiss: ${alert.title ?? alert.message}`}
              onClick={() => onDismiss(alert)}
            >
              Dismiss
            </button>
          )}
        </div>
      ))}
    </section>
  );
}
