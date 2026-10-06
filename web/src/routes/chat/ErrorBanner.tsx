/** The per-request error banner above the composer (docs/ui/07 §10). */
import { Link } from 'wouter-preact';
import { api } from '../../api/client';
import { Banner } from '../../components/Banner';
import { Button } from '../../components/Button';
import { toastError } from '../../components/Toast';
import { isCancelled, withInstallConfirm } from '../../lib/engine-install';
import { t } from '../../strings/chat';
import type { ChatError } from './logic';
import type { PanelTab } from './SidePanel';

export type ChatErrorState = ChatError & { request?: unknown; retries: number; retryIn: number | null };

const RETRYABLE = ['unreachable', 'recovering', 'busy', 'queue_full', 'resource_timeout', 'request_timeout', 'mask_timeout', 'generic', 'capacity'];

export interface ErrorBannerProps {
  error: ChatErrorState;
  onRetry: () => void;
  onSwitchWhenIdle: () => void;
  onOpenTab: (tab: PanelTab) => void;
  onTurnOffEos: () => void;
  onRemoveAttachments: () => void;
  onShowRequest: () => void;
  onDismiss: () => void;
}

export function ErrorBanner({ error, onRetry, onSwitchWhenIdle, onOpenTab, onTurnOffEos, onRemoveAttachments, onShowRequest, onDismiss }: ErrorBannerProps) {
  return (
    <Banner
      tone={error.kind === 'failed' ? 'critical' : 'warn'}
      title={error.title}
      actions={
        <span class="cluster">
          {error.retryIn !== null ? (
            <Button size="s" onClick={onRetry}>
              {t('chat.action.retry_in', { s: error.retryIn })}
            </Button>
          ) : (
            RETRYABLE.includes(error.kind) && (
              <Button size="s" onClick={onRetry}>
                {t('chat.action.retry')}
              </Button>
            )
          )}
          {error.kind === 'busy' && (
            <Button size="s" variant="text" onClick={onSwitchWhenIdle}>
              {t('chat.action.switch_idle')}
            </Button>
          )}
          {error.kind === 'failed' && (
            <Button
              size="s"
              variant="solid"
              onClick={() =>
                void withInstallConfirm((force) => api.post(force ? '/engine/restart?force=true' : '/engine/restart')).catch((err) => isCancelled(err) || toastError(t('chat.restart_failed'), err))
              }
            >
              {t('chat.action.restart')}
            </Button>
          )}
          {(error.kind === 'failed' || error.kind === 'generic') && (
            <Link href="/logs" class="btn" data-variant="text" data-size="s">
              {t('chat.action.logs')}
            </Link>
          )}
          {(error.kind === 'capacity' || error.kind === 'resource_timeout') && (
            <Link href="/settings/memory" class="btn" data-variant="text" data-size="s">
              {t('chat.action.memory')}
            </Link>
          )}
          {error.kind === 'queue_full' && (
            <Link href="/settings/requests" class="btn" data-variant="text" data-size="s">
              {t('chat.action.requests')}
            </Link>
          )}
          {error.kind === 'mask_timeout' && (
            <Button size="s" variant="text" onClick={() => onOpenTab('output')}>
              {t('chat.action.output')}
            </Button>
          )}
          {error.kind === 'later_system' && (
            <Button size="s" variant="text" onClick={() => onOpenTab('system')}>
              {t('chat.action.system')}
            </Button>
          )}
          {error.kind === 'ignore_eos' && (
            <Button size="s" variant="text" onClick={onTurnOffEos}>
              {t('chat.action.turn_off_eos')}
            </Button>
          )}
          {error.kind === 'attachment' && (
            <Button size="s" variant="text" onClick={onRemoveAttachments}>
              {t('chat.action.remove_attachments')}
            </Button>
          )}
          {error.kind === 'rejected' && error.request != null && (
            <Button size="s" variant="text" onClick={onShowRequest}>
              {t('chat.action.show_request')}
            </Button>
          )}
          <Button size="s" variant="text" onClick={onDismiss}>
            {error.retryIn !== null ? t('chat.action.cancel') : t('chat.action.dismiss')}
          </Button>
        </span>
      }
    >
      {error.body && <span>{error.body} </span>}
      {error.detail && <code class="mono">{error.detail}</code>}
    </Banner>
  );
}
