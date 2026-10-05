import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import type uPlot from 'uplot';
import { Chart, type ChartSeries } from '../../components/Chart';
import { SegmentedControl } from '../../components/controls';
import { LoadError } from '../../components/States';
import { DASH, formatBytes, formatBytesPerSecond, formatCount, formatMs, formatPercent, formatTokPerSec } from '../../lib/format';
import { useLiveMetrics } from '../../store/live';
import { t } from '../../strings/status';
import { metricsSeries } from './api';
import { append, emptyBuffer, fromSeries, lastAndPeak, MAX_WINDOW_S, merge, WINDOWS, windowData, type SeriesBuffer, type SeriesKey, type WindowMin } from './series';

type Fmt = (v: number) => string;

interface ChartDef {
  id: string;
  title: string;
  keys: SeriesKey[];
  series: ChartSeries[];
  yFormat: Fmt;
  /** Current value shown at the right of the title. */
  current: (lasts: Array<number | null>) => string;
  /** Fractions shown as percents: a fixed 0–1 axis. */
  yRange?: [number, number];
}

const tps: Fmt = (v) => formatTokPerSec(v, { unit: false });
const pct: Fmt = (v) => formatPercent(v, 0);
const count: Fmt = (v) => formatCount(v);
const bytes: Fmt = (v) => formatBytes(v);

export function defs(): ChartDef[] {
  const s = (key: Parameters<typeof t>[0], opts: Partial<ChartSeries> = {}): ChartSeries => ({ label: t(key), ...opts });
  return [
    {
      id: 'decode',
      title: t('status.charts.decode'),
      keys: ['throughput.decode_tps'],
      series: [s('status.charts.s.decode', { primary: true })],
      yFormat: tps,
      current: ([v]) => (v == null ? DASH : formatTokPerSec(v)),
    },
    {
      id: 'prefill',
      title: t('status.charts.prefill'),
      keys: ['throughput.prefill_tps'],
      series: [s('status.charts.s.prefill', { primary: true })],
      yFormat: tps,
      current: ([v]) => (v == null ? DASH : formatTokPerSec(v)),
    },
    {
      id: 'lanes',
      title: t('status.charts.lanes'),
      keys: ['scheduler.decoding', 'scheduler.prefilling', 'scheduler.queued', 'scheduler.waiting_resources'],
      series: [
        s('status.charts.s.decoding', { primary: true }),
        s('status.charts.s.prefilling'),
        s('status.charts.s.queued', { dashed: true }),
        s('status.charts.s.waiting', { dashed: true }),
      ],
      yFormat: count,
      current: ([d, p, q]) => (d == null && p == null ? DASH : `${formatCount((d ?? 0) + (p ?? 0))} / ${formatCount(q ?? 0)}`),
    },
    {
      id: 'ttft',
      title: t('status.charts.ttft'),
      keys: ['latency.ttft_p50_ms', 'latency.ttft_p95_ms'],
      series: [s('status.charts.s.p50', { primary: true }), s('status.charts.s.p95', { dashed: true })],
      yFormat: formatMs,
      current: ([p50, p95]) => (p50 == null ? DASH : `${formatMs(p50)} / ${formatMs(p95)}`),
    },
    {
      id: 'memory',
      title: t('status.charts.memory'),
      keys: ['memory.charged_bytes', 'memory.current_bytes', 'memory.peak_bytes', 'memory.limit_bytes'],
      series: [
        s('status.charts.s.charged', { primary: true }),
        s('status.charts.s.current'),
        s('status.charts.s.peak', { dashed: true }),
        s('status.charts.s.limit', { dashed: true }),
      ],
      yFormat: bytes,
      current: ([c]) => (c == null ? DASH : formatBytes(c)),
    },
    {
      id: 'kv',
      title: t('status.charts.kv'),
      keys: ['kv.pages_active', 'kv.pages_cache', 'kv.pages_free'],
      series: [s('status.charts.s.active', { primary: true }), s('status.charts.s.cache'), s('status.charts.s.free', { dashed: true })],
      yFormat: count,
      current: ([a, c, f]) => (a == null ? DASH : `${formatCount(a)} / ${formatCount((a ?? 0) + (c ?? 0) + (f ?? 0))}`),
    },
    {
      id: 'hit',
      title: t('status.charts.hit'),
      keys: ['cache.hit_rate', 'cache.efficiency'],
      series: [s('status.charts.s.hit', { primary: true }), s('status.charts.s.eff', { dashed: true })],
      yFormat: pct,
      yRange: [0, 1],
      current: ([h]) => (h == null ? DASH : formatPercent(h, 0)),
    },
    {
      id: 'disk',
      title: t('status.charts.disk'),
      keys: ['disk.read_bps', 'disk.written_bps'],
      series: [s('status.charts.s.read', { primary: true }), s('status.charts.s.written')],
      yFormat: (v) => formatBytesPerSecond(v),
      current: ([r, w]) => (r == null && w == null ? DASH : `${formatBytesPerSecond(r ?? 0)} / ${formatBytesPerSecond(w ?? 0)}`),
    },
  ];
}

function prefersReducedMotion(): boolean {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

const clock = (sec: number) => new Date(sec * 1000).toLocaleTimeString('en-GB');

export interface ChartsBandProps {
  windowMin: WindowMin;
  onWindow: (w: WindowMin) => void;
  /** SSD tier on (`disk.capacity_bytes > 0` or `serve.max_cache_disk` set). */
  diskEnabled: boolean;
  /** `scheduler` group reported by this Splash version. */
  lanesReported: boolean;
}

/** Live charts band (docs/ui/02 §7): backfill from /metrics/series, then live samples. */
export function ChartsBand({ windowMin, onWindow, diskEnabled, lanesReported }: ChartsBandProps) {
  const buffer = useRef<SeriesBuffer>(emptyBuffer());
  const dirty = useRef(true);
  const [tick, setTick] = useState(0);
  const [paused, setPaused] = useState(false);
  const [backfillError, setBackfillError] = useState<unknown>(null);
  const [valuesOpen, setValuesOpen] = useState(false);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const backfill = () => {
    setBackfillError(null);
    metricsSeries(MAX_WINDOW_S)
      .then((series) => {
        buffer.current = merge(fromSeries(series), buffer.current);
        dirty.current = true;
      })
      .catch((err: unknown) => setBackfillError(err));
  };
  useEffect(backfill, []);

  useLiveMetrics((sample) => {
    if (append(buffer.current, sample)) dirty.current = true;
  });

  useEffect(() => {
    // Decision T2: redraw every 2 s under reduced motion.
    const id = setInterval(
      () => {
        if (dirty.current && !pausedRef.current) {
          dirty.current = false;
          setTick((n) => n + 1);
        }
      },
      prefersReducedMotion() ? 2000 : 1000,
    );
    return () => clearInterval(id);
  }, []);

  const all = useMemo(defs, []);
  const shown = all.filter((d) => (d.id !== 'lanes' || lanesReported) && (d.id !== 'disk' || diskEnabled));
  const view = useMemo(() => {
    const buf = buffer.current;
    const now = buf.t[buf.t.length - 1] ?? Date.now() / 1000;
    const windowS = windowMin * 60;
    return shown.map((d) => {
      const data = windowData(buf, d.keys, windowS, Math.max(now, Date.now() / 1000 - 1));
      const stats = data.slice(1).map((col) => lastAndPeak(col as Array<number | null>));
      return { d, data: data as unknown as uPlot.AlignedData, lasts: stats.map((s) => s.last), peak: stats[0]?.peak ?? null };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, windowMin, shown.length]);

  return (
    <section class="band status-charts" aria-labelledby="status-charts-label" data-testid="charts-band">
      <div class="cluster status-band-head">
        <div class="stack" style={{ gap: '4px' }}>
          <h2 class="label" id="status-charts-label">
            {t('status.charts.label')}
          </h2>
          {(paused || !lanesReported) && (
            <p class="meta">{[paused ? t('status.charts.paused') : null, !lanesReported ? t('status.charts.lanes_missing') : null].filter(Boolean).join(' · ')}</p>
          )}
        </div>
        <div class="cluster">
          <SegmentedControl<string>
            label={t('status.charts.window')}
            size="s"
            value={String(windowMin)}
            onChange={(v) => onWindow(Number(v) as WindowMin)}
            options={WINDOWS.map((w) => ({ value: String(w), label: t('status.charts.window_opt', { n: w }) }))}
          />
          <button type="button" class="btn" data-variant="text" data-size="s" aria-pressed={paused} onClick={() => setPaused(!paused)}>
            {paused ? t('status.charts.follow') : t('status.charts.pause')}
          </button>
        </div>
      </div>
      {backfillError ? <LoadError thing={t('status.charts.backfill_failed')} error={backfillError} onRetry={backfill} /> : null}
      <div class="status-chart-grid">
        {view.map(({ d, data, lasts, peak }) => {
          const now = d.current(lasts);
          return (
            <div class="status-chart" key={d.id} data-chart={d.id}>
              <Chart
                title={d.title}
                summary={now}
                ariaLabel={t('status.charts.aria', { title: d.title, n: windowMin, now, peak: peak == null ? DASH : d.yFormat(peak) })}
                data={data}
                series={d.series}
                height={160}
                yFormat={d.yFormat}
                {...(d.yRange ? { yRange: d.yRange } : {})}
                empty={t('status.charts.no_samples')}
              />
            </div>
          );
        })}
        {!diskEnabled && (
          <div class="status-chart status-chart-note" data-chart="disk-off">
            <p class="label">{t('status.charts.disk')}</p>
            <p class="body">
              {t('status.charts.ssd_off')} ·{' '}
              <Link href="/settings/cache#serve.max_cache_disk" class="status-link">
                {t('status.cache.settings')}
              </Link>
            </p>
          </div>
        )}
      </div>
      <details class="disclosure status-values" onToggle={(e) => setValuesOpen((e.currentTarget as HTMLDetailsElement).open)}>
        <summary class="label">
          <span class="disclosure-glyph" aria-hidden="true">
            ▸
          </span>
          {t('status.charts.values')}
        </summary>
        {valuesOpen && <ValuesTable buffer={buffer.current} defs={shown} tick={tick} />}
      </details>
    </section>
  );
}

function ValuesTable({ buffer, defs: list }: { buffer: SeriesBuffer; defs: ChartDef[]; tick: number }) {
  const n = buffer.t.length;
  const rows = Array.from({ length: Math.min(10, n) }, (_, k) => n - 1 - k);
  const cols = list.flatMap((d) => d.keys.map((key, i) => ({ key, label: `${d.title} · ${d.series[i]?.label ?? ''}`, fmt: d.yFormat })));
  return (
    <div class="table-wrap disclosure-body">
      <table class="table">
        <caption class="visually-hidden">{t('status.charts.values_caption')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('status.charts.time')}</th>
            {cols.map((c) => (
              <th key={c.key} scope="col" data-align="right">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((i) => (
            <tr key={i}>
              <td class="tnum">{clock(buffer.t[i] ?? 0)}</td>
              {cols.map((c) => {
                const v = buffer.cols[c.key][i];
                return (
                  <td key={c.key} data-align="right">
                    {v == null ? DASH : c.fmt(v)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
