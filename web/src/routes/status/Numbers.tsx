import type { ComponentChildren, RefObject } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import type { LiveMetrics, UsageSummary } from '../../api/models';
import { SegmentedControl } from '../../components/controls';
import { LoadError } from '../../components/States';
import { Stat } from '../../components/NumbersBand';
import { toastError } from '../../components/Toast';
import { DASH, formatCompact, formatCount, formatDate, formatDuration, formatMs, formatPercent, formatTokPerSec } from '../../lib/format';
import { useApi } from '../../lib/use-api';
import { t } from '../../strings/status';
import { resetMetrics, usageSummary } from './api';
import { rawNum } from './logic';

export type Scope = 'session' | 'all';

export interface StatModel {
  key: string;
  label: string;
  value: string;
  unit?: string;
  sub?: ComponentChildren;
  /** Full value for the tooltip. */
  title?: string;
}

const compactTps = (v: number | null | undefined, narrow: boolean) =>
  v == null ? DASH : narrow && v >= 1000 ? formatCompact(v) : formatTokPerSec(v, { unit: false });

function requestsSub(failed: number | null | undefined, cancelled: number | null | undefined): ComponentChildren {
  if (failed == null && cancelled == null) return undefined;
  const f = failed ?? 0;
  return (
    <>
      <span class={f > 0 ? 'acc' : undefined} data-accent={f > 0 ? 'true' : undefined} data-testid="requests-failed">
        {t('status.numbers.failed', { n: formatCount(f) })}
      </span>
      {cancelled != null && <> · {t('status.numbers.cancelled', { n: formatCount(cancelled) })}</>}
    </>
  );
}

/** Eight stats "since engine start" from a live sample (+ optional raw /status for sub-lines). */
export function sessionStats(s: LiveMetrics | null, raw: unknown, narrow = false): StatModel[] {
  const tt = s?.totals;
  const kvHit = rawNum(raw, 'cache.kv_hit_tokens');
  const diskHit = rawNum(raw, 'cache.kv_disk_hit_tokens');
  const wall = rawNum(raw, 'metrics.prefill_wall_ms');
  const unit = t('status.numbers.unit_tps');
  // Splash reports 0 for TTFT and acceptance before anything ran: that is "no data" (—), not 0 ms / 0%.
  const noRequests = !tt?.requests_completed;
  const noDrafts = !s?.draft?.drafted_tokens;
  return [
    {
      key: 'tokens',
      label: t('status.numbers.tokens'),
      value: formatCompact(tt?.total_tokens),
      title: formatCount(tt?.total_tokens),
      sub: s ? t('status.numbers.in_out', { i: formatCompact(tt?.prompt_tokens), o: formatCompact(tt?.decode_tokens) }) : undefined,
    },
    {
      key: 'cached',
      label: t('status.numbers.cached'),
      value: formatCompact(tt?.reused_tokens),
      title: formatCount(tt?.reused_tokens),
      sub: kvHit !== null || diskHit !== null ? t('status.numbers.kv_disk', { kv: formatCompact(kvHit), disk: formatCompact(diskHit) }) : undefined,
    },
    {
      key: 'efficiency',
      label: t('status.numbers.efficiency'),
      value: formatPercent(s?.cache?.efficiency, 1),
      title: t('status.numbers.eff_help'),
      sub: s?.cache?.hit_rate != null ? t('status.numbers.hit_rate', { v: formatPercent(s.cache.hit_rate, 0) }) : undefined,
    },
    {
      key: 'decode',
      label: t('status.numbers.decode'),
      value: compactTps(s?.throughput?.decode_tps, narrow),
      unit,
      sub: s ? t('status.numbers.cycle', { v: s.throughput?.decode_tps_cycle != null ? formatTokPerSec(s.throughput.decode_tps_cycle) : DASH }) : undefined,
    },
    {
      key: 'prefill',
      label: t('status.numbers.prefill'),
      value: compactTps(s?.throughput?.prefill_tps, narrow),
      unit,
      sub: wall !== null ? t('status.numbers.wall', { v: formatMs(wall) }) : undefined,
    },
    {
      key: 'draft',
      label: t('status.numbers.draft'),
      value: noDrafts ? DASH : formatPercent(s?.draft?.acceptance_rate, 0),
      sub:
        s?.draft?.drafted_tokens != null
          ? t('status.numbers.accepted', { a: formatCompact(s.draft.accepted_tokens), d: formatCompact(s.draft.drafted_tokens) })
          : undefined,
    },
    {
      key: 'ttft',
      label: t('status.numbers.ttft'),
      value: noRequests ? DASH : formatMs(s?.latency?.ttft_p50_ms),
      sub: s ? t('status.numbers.p95', { v: noRequests ? DASH : formatMs(s.latency?.ttft_p95_ms) }) : undefined,
    },
    {
      key: 'requests',
      label: t('status.numbers.requests'),
      value: narrow && (tt?.requests_completed ?? 0) >= 10000 ? formatCompact(tt?.requests_completed) : formatCount(tt?.requests_completed),
      title: formatCount(tt?.requests_completed),
      sub: s ? requestsSub(tt?.requests_failed, tt?.requests_cancelled) : undefined,
    },
  ];
}

/** The same eight stats from usage.db (`GET /usage/summary?scope=all`). */
export function usageStats(u: UsageSummary | null, narrow = false): StatModel[] {
  const engineOnly = u ? t('status.numbers.engine_only') : undefined;
  const cancelled = u ? Math.max(0, u.requests - u.completed - u.failed) : null;
  const unit = t('status.numbers.unit_tps');
  return [
    {
      key: 'tokens',
      label: t('status.numbers.tokens'),
      value: formatCompact(u?.total_tokens),
      title: formatCount(u?.total_tokens),
      sub: u ? t('status.numbers.in_out', { i: formatCompact(u.prompt_tokens), o: formatCompact(u.completion_tokens) }) : undefined,
    },
    { key: 'cached', label: t('status.numbers.cached'), value: formatCompact(u?.cached_tokens), title: formatCount(u?.cached_tokens) },
    { key: 'efficiency', label: t('status.numbers.efficiency'), value: formatPercent(u?.cache_efficiency, 1), title: t('status.numbers.eff_help') },
    { key: 'decode', label: t('status.numbers.decode'), value: DASH, unit, sub: engineOnly },
    { key: 'prefill', label: t('status.numbers.prefill'), value: DASH, unit, sub: engineOnly },
    { key: 'draft', label: t('status.numbers.draft'), value: DASH, sub: engineOnly },
    {
      key: 'ttft',
      label: t('status.numbers.ttft'),
      value: u?.completed ? formatMs(u.ttft_p50_ms) : DASH,
      sub: u ? t('status.numbers.p95', { v: u.completed ? formatMs(u.ttft_p95_ms) : DASH }) : undefined,
    },
    {
      key: 'requests',
      label: t('status.numbers.requests'),
      value: narrow && (u?.completed ?? 0) >= 10000 ? formatCompact(u?.completed) : formatCount(u?.completed),
      title: formatCount(u?.completed),
      sub: u ? requestsSub(u.failed, cancelled) : undefined,
    },
  ];
}

/** Decision T1: compact numerals when a stat column is narrower than 180px. */
function useNarrow(): [RefObject<HTMLDivElement | null>, boolean] {
  const ref = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const check = () => {
      const first = el.querySelector<HTMLElement>('.stat');
      if (first) setNarrow(first.getBoundingClientRect().width < 180);
    };
    const ro = new ResizeObserver(check);
    ro.observe(el);
    check();
    return () => ro.disconnect();
  }, []);
  return [ref, narrow];
}

const timeOf = (ms: number) => new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

function readCleared(): number | null {
  try {
    const v = Number(sessionStorage.getItem('splash-gui-status-cleared'));
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
}

export interface NumbersProps {
  scope: Scope;
  onScope: (scope: Scope) => void;
  sample: LiveMetrics | null;
  sampleAt: number | null;
  raw: unknown;
  /** Engine stopped: the session scope shows — everywhere (decision T9). */
  stopped: boolean;
  /** recovering / engine_failed / stopping / crashed: last values muted. */
  frozen: boolean;
}

/** Numbers band (docs/ui/02 §5). */
export function NumbersBand({ scope, onScope, sample, sampleAt, raw, stopped, frozen }: NumbersProps) {
  const [gridRef, narrow] = useNarrow();
  const [cleared, setCleared] = useState<number | null>(readCleared);
  const [restartedAt, setRestartedAt] = useState<number | null>(null);
  const [clearing, setClearing] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const summary = useApi((signal) => usageSummary({ scope: 'all' }, signal), [scope], scope === 'all');

  useEffect(() => {
    if (scope !== 'all') return;
    const id = setInterval(() => void summary.reload(), 30_000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);
  useEffect(() => {
    if (sample?.restarted) setRestartedAt(Date.now());
  }, [sample]);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const session = scope === 'session';
  const liveSample = session && !stopped ? sample : null;
  const stats = session ? sessionStats(liveSample, raw, narrow) : usageStats(summary.data, narrow);
  const ageS = sampleAt ? Math.round((now - sampleAt) / 1000) : null;
  const stale = session && !stopped && (frozen || (ageS !== null && ageS > 5));

  const metas: string[] = [];
  if (session && stopped) metas.push(t('status.numbers.stopped'));
  if (session && !stopped && cleared) metas.push(t('status.numbers.cleared', { time: timeOf(cleared) }));
  if (session && !stopped && restartedAt) metas.push(t('status.numbers.restarted', { time: timeOf(restartedAt) }));
  if (stale && ageS !== null) metas.push(t('status.numbers.stale', { age: formatDuration(ageS) }));
  if (!session && summary.data?.since) metas.push(t('status.numbers.usage_since', { date: formatDate(Date.parse(summary.data.since)) }));

  const clear = async () => {
    setClearing(true);
    try {
      await resetMetrics();
      const at = Date.now();
      setCleared(at);
      setRestartedAt(null);
      try {
        sessionStorage.setItem('splash-gui-status-cleared', String(at));
      } catch {
        /* per-tab only */
      }
    } catch (err) {
      toastError(t('status.numbers.clear_failed'), err);
    } finally {
      setClearing(false);
    }
  };

  return (
    <section class="band status-numbers" aria-labelledby="status-numbers-label" data-testid="numbers-band">
      <div class="cluster status-band-head">
        <div class="stack" style={{ gap: '4px' }}>
          <h2 class="label" id="status-numbers-label">
            {t('status.numbers.label')}
          </h2>
          {metas.length > 0 && <p class="meta">{metas.join(' · ')}</p>}
        </div>
        <div class="cluster">
          <SegmentedControl<Scope>
            label={t('status.numbers.scope')}
            size="s"
            value={scope}
            onChange={onScope}
            options={[
              { value: 'session', label: t('status.numbers.session') },
              { value: 'all', label: t('status.numbers.all') },
            ]}
          />
          {session && (
            <button type="button" class="btn" data-variant="text" data-size="s" disabled={stopped || clearing} aria-busy={clearing ? 'true' : undefined} onClick={() => void clear()}>
              {t('status.numbers.clear')}
            </button>
          )}
        </div>
      </div>
      {!session && summary.error ? <LoadError thing={t('status.numbers.all')} error={summary.error} onRetry={() => void summary.reload()} /> : null}
      <div class="numbers-clip" ref={gridRef}>
        <div class="numbers-grid numbers status-numbers-grid" data-stale={stale ? 'true' : undefined}>
          {stats.map((s) => (
            <div key={s.key} class="status-stat" data-stat={s.key}>
              <Stat label={s.label} value={s.value} unit={s.value === DASH ? undefined : s.unit} sub={s.sub} title={s.title} />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
