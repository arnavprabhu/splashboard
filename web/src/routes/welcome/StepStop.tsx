/**
 * The stop state: this Mac cannot run Splash, so the wizard says why
 * in place of its steps. The rail is hidden and no step can be reached; the actions are the links
 * that help with the cause (no Continue, and no accent button except Software Update).
 */

import type { TargetedMouseEvent } from 'preact';
import { Button, toastError } from '../../components';
import { t } from '../../strings/welcome';
import { openURL } from './host';
import type { StopReason } from './logic';

/** The requirements in the Splash README (its "Quick start" section; https://github.com/incoai/splash). */
export const REQUIREMENTS_URL = 'https://github.com/incoai/splash#quick-start';
/** The Software Update pane (the scheme Settings already uses for System Settings panes). */
export const SOFTWARE_UPDATE_URL = 'x-apple.systempreferences:com.apple.Software-Update-Settings.extension';

export interface StopStateProps {
  reason: StopReason;
  /** In the menu bar app the links go through its bridge, because the page never opens new tabs. */
  hosted: boolean;
  /** Re-reads /system, so the wizard continues once the update is installed. */
  onCheck: () => void;
}

/** Click handler for a link that the app window must open itself; a browser follows the link. */
function viaHost(hosted: boolean, href: string) {
  return (event: TargetedMouseEvent<HTMLAnchorElement>) => {
    if (!hosted) return;
    event.preventDefault();
    openURL(href).catch((err: unknown) => toastError(t('welcome.stop.open_failed'), err));
  };
}

export function StopState({ reason, hosted, onCheck }: StopStateProps) {
  const chip = reason.kind === 'chip';
  return (
    <section class="band" data-testid="stop-state" data-kind={reason.kind} aria-labelledby="wz-stop-title">
      <div class="stack">
        <h2 id="wz-stop-title" class="display-m">
          {chip ? t('welcome.stop.chip.title') : t('welcome.stop.mac.title')}
        </h2>
        <p class="lead">{chip ? t('welcome.stop.chip.lead', { chip: reason.chip ?? t('welcome.unknown_chip') }) : t('welcome.stop.mac.lead', { macos: reason.macos })}</p>
        <p class="body">{chip ? t('welcome.stop.chip.body') : t('welcome.stop.mac.body')}</p>
        <div class="cluster">
          {chip ? (
            <a
              class="btn"
              data-variant="outline"
              data-size="m"
              href={REQUIREMENTS_URL}
              target={hosted ? undefined : '_blank'}
              rel="noopener noreferrer"
              onClick={viaHost(hosted, REQUIREMENTS_URL)}
              data-testid="stop-requirements"
            >
              {t('welcome.stop.chip.requirements')} ↗
            </a>
          ) : (
            <>
              <a class="btn" data-variant="accent" data-size="m" href={SOFTWARE_UPDATE_URL} onClick={viaHost(hosted, SOFTWARE_UPDATE_URL)} data-testid="stop-software-update">
                {t('welcome.stop.mac.update')}
              </a>
              <Button variant="outline" onClick={onCheck} data-testid="stop-check">
                {t('welcome.engine.check_again')}
              </Button>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
