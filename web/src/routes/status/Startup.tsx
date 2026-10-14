import { useEffect, useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import type { EngineSummary } from '../../api/types';
import { Banner } from '../../components/Banner';
import { Install } from '../../components/Install';
import { LogPane } from '../../components/LogPane';
import { installOf } from '../../lib/engine-install';
import { Section } from '../../components/Section';
import { t } from '../../strings/status';
import { viewOf } from './logic';

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

/** Banners under the header: recovering, engine failed, crashed. */
export function StateBanners({ engine: e }: { engine: EngineSummary | null }) {
  const view = viewOf(e?.view);
  const state = e?.state ?? null;
  const countdown = useCountdown(state === 'crashed' ? view.restart?.next_retry_at : null);
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
  if (out.length === 0) return null;
  return <div class="status-banners stack">{out}</div>;
}

/** Replaces the numbers while starting: phase headline plus the engine's last lines. */
export function StartupBand({ engine: e }: { engine: EngineSummary }) {
  const view = viewOf(e.view);
  const phase = e.phase ?? 'loading';
  const lines = (view.log_tail ?? []).slice(-12);
  const install = installOf(e);
  const hub = (view.notices ?? []).filter((n) => n.kind === 'hub_unreachable' || n.kind === 'new_commit_not_installed');
  return (
    <Section label={t('status.startup.label')} id="status-startup">
      <div class="stack">
        <p class="lead loading-dots" data-testid="startup-headline">
          {t(phase === 'installing' ? 'status.startup.installing' : phase === 'warming' ? 'status.startup.warming' : 'status.startup.loading').replace(/…$/, '')}
        </p>
        {install && <Install install={install} />}
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

