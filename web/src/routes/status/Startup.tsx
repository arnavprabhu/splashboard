import { useEffect, useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import type { EngineSummary } from '../../api/types';
import { Banner } from '../../components/Banner';
import { Button } from '../../components/Button';
import { LogPane } from '../../components/LogPane';
import { Section } from '../../components/Section';
import { toast, toastError } from '../../components/Toast';
import { formatBytes, formatCount } from '../../lib/format';
import { t } from '../../strings/status';
import { runEngineCall } from './actions';
import { savePatch } from './api';
import { sessionKey } from './Header';
import { diskNotice, parseDiskSuggestion, ssdCacheSize, viewOf } from './logic';

function readDismissed(key: string): boolean {
  try {
    return sessionStorage.getItem(`splash-gui-ssd-dismissed:${key}`) === '1';
  } catch {
    return false;
  }
}

function useCountdown(until: string | null | undefined): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!until) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [until]);
  if (!until) return null;
  const at = Date.parse(until);
  return Number.isFinite(at) ? Math.max(0, Math.ceil((at - now) / 1000)) : null;
}

/** Banners under the header: recovering, engine failed, crashed, SSD suggestion (docs/ui/02 §10, §11.2). */
export function StateBanners({ engine: e, persistentCache }: { engine: EngineSummary | null; persistentCache: boolean }) {
  const view = viewOf(e?.view);
  const state = e?.state ?? null;
  const key = sessionKey(e);
  const [dismissed, setDismissed] = useState(() => readDismissed(key));
  const [enabling, setEnabling] = useState(false);
  useEffect(() => setDismissed(readDismissed(key)), [key]);
  const countdown = useCountdown(state === 'crashed' ? view.restart?.next_retry_at : null);
  const notice = diskNotice(view.notices);
  const showDisk = notice && !dismissed && (state === 'ready' || state === 'busy' || state === 'idle_released');
  const size = ssdCacheSize(persistentCache);
  const out = [];

  if (state === 'recovering') {
    out.push(
      <Banner key="rec" tone="warn" title={t('status.banner.recovering')}>
        {view.transport?.error && <code class="mono">{view.transport.error}</code>}
      </Banner>,
    );
  }
  if (state === 'engine_failed') {
    out.push(
      <Banner
        key="ef"
        tone="critical"
        title={t('status.banner.engine_failed')}
        actions={
          <Link href="/logs?source=engine&level=error" class="btn" data-variant="text" data-size="s">
            {t('status.actions.open_logs')}
          </Link>
        }
      >
        <span>{t('status.banner.engine_failed_msg')} </span>
        {view.transport?.error && <code class="mono">{view.transport.error}</code>}
      </Banner>,
    );
  }
  if (state === 'crashed') {
    const n = Math.max(1, view.restart?.attempt ?? 1);
    out.push(
      <Banner
        key="cr"
        tone="warn"
        title={t('status.banner.crashed')}
        actions={
          <Link href="/logs?source=engine&level=error" class="btn" data-variant="text" data-size="s">
            {t('status.actions.open_logs')}
          </Link>
        }
      >
        {countdown !== null ? t('status.banner.crashed_msg', { s: countdown, n }) : t('status.banner.crashed_msg_now', { n })}
      </Banner>,
    );
  }
  if (showDisk && notice) {
    const parsed = parseDiskSuggestion(notice.raw ?? notice.message);
    const enable = async () => {
      setEnabling(true);
      try {
        await savePatch({ 'serve.max_cache_disk': size }, null);
      } catch (err) {
        toastError(t('status.banner.ssd_failed'), err);
        setEnabling(false);
        return;
      }
      toast(t('status.banner.ssd_done', { size }));
      // runEngineCall reports its own failure.
      await runEngineCall({ kind: 'restart' }).catch(() => undefined);
      setEnabling(false);
    };
    out.push(
      <Banner
        key="ssd"
        tone="info"
        title={t('status.banner.ssd_title')}
        actions={
          <span class="cluster" style={{ gap: '8px 16px' }}>
            <Button size="s" loading={enabling} onClick={() => void enable()} data-testid="ssd-enable">
              {t('status.banner.ssd_action', { size })}
            </Button>
            <Link href="/settings/cache#serve.max_cache_disk" class="btn" data-variant="text" data-size="s">
              {t('status.banner.ssd_settings')}
            </Link>
            <Button
              size="s"
              variant="text"
              onClick={() => {
                try {
                  sessionStorage.setItem(`splash-gui-ssd-dismissed:${key}`, '1');
                } catch {
                  /* per-tab only */
                }
                setDismissed(true);
              }}
            >
              {t('status.banner.ssd_dismiss')}
            </Button>
          </span>
        }
      >
        {parsed.availableBytes !== null && parsed.tokens !== null ? (
          t('status.banner.ssd_msg', { avail: formatBytes(parsed.availableBytes), tokens: formatCount(parsed.tokens) })
        ) : (
          <code class="mono">{notice.message}</code>
        )}
      </Banner>,
    );
  }
  if (out.length === 0) return null;
  return <div class="status-banners stack">{out}</div>;
}

/** Replaces the numbers while starting: phase headline plus the engine's last lines (docs/ui/02 §10). */
export function StartupBand({ engine: e }: { engine: EngineSummary }) {
  const view = viewOf(e.view);
  const phase = e.phase ?? 'loading';
  const lines = (view.log_tail ?? []).slice(-12);
  const hub = (view.notices ?? []).filter((n) => n.kind === 'hub_unreachable' || n.kind === 'new_commit_not_installed');
  return (
    <Section label={t('status.startup.label')} id="status-startup">
      <div class="stack">
        <p class="lead loading-dots" data-testid="startup-headline">
          {t(phase === 'installing' ? 'status.startup.installing' : phase === 'warming' ? 'status.startup.warming' : 'status.startup.loading').replace(/…$/, '')}
        </p>
        {hub.map((n) => (
          <Banner key={n.ts + n.kind} tone="info" title={t('status.banner.hub')}>
            <code class="mono">{n.message}</code>
          </Banner>
        ))}
        <LogPane
          label={t('status.startup.log')}
          height={260}
          lines={lines.map((text, i) => ({ key: `${i}-${text}`, text }))}
          empty={t('status.startup.empty')}
        />
      </div>
    </Section>
  );
}

