import { useEffect, useRef, useState } from 'preact/hooks';
import type uPlot from 'uplot';
import { effectiveTheme } from '../store/theme';

export interface ChartSeries {
  label: string;
  /** The primary series is drawn in --acc; the rest in --ink. */
  primary?: boolean;
  /** Dashed ink line for secondary references such as p95 or a limit. */
  dashed?: boolean;
  /** Overrides the colour: `mute` for "other" groups and flat limits. */
  tone?: 'acc' | 'ink' | 'mute';
}

export interface ChartProps {
  title?: string;
  ariaLabel?: string;
  /** x values (unix seconds) first, then one array per series. */
  data: uPlot.AlignedData;
  series: readonly ChartSeries[];
  height?: number;
  yFormat?: (value: number) => string;
  /** Accessible summary of the latest values. */
  summary?: string;
  /** Shown over empty axes when there are no samples ("No samples yet"). */
  empty?: string;
  /** Formats a sample for the hidden values table (defaults to yFormat). */
  valueFormat?: (value: number) => string;
  /** x is unix seconds and the axis shows time (default); false for category/ordinal x. */
  time?: boolean;
  /** Each x is a whole local day (usage history): the hidden table shows dates, not times. */
  days?: boolean;
  /** Each column is cumulative over the columns after it (stacked bars); the hidden table shows each series' own value. */
  stacked?: boolean;
  /** Bars instead of lines (usage history). */
  bars?: boolean;
  /** A fixed y range, e.g. [0, 1] for fractions shown as percents (uPlot's empty auto-range is 0–100). */
  yRange?: [number, number];
}

/** The hidden table's label for one x value. */
export function xLabel(x: number, props: Pick<ChartProps, 'time' | 'days'>): string {
  if (props.time === false) return String(x);
  const d = new Date(x * 1000);
  return props.days ? d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : d.toLocaleTimeString('en-GB');
}

let loader: Promise<typeof uPlot> | null = null;
let bars: uPlot.Series.PathBuilder | null = null;
let UPlotRef: typeof uPlot | null = null;
let measure: CanvasRenderingContext2D | null | undefined;

function barsPath(): uPlot.Series.PathBuilder | undefined {
  if (!UPlotRef) return undefined;
  bars ??= UPlotRef.paths.bars?.({ size: [0.7, 64] }) ?? null;
  return bars ?? undefined;
}
/** uPlot is code-split and only fetched by pages that draw a chart. */
export function loadUPlot(): Promise<typeof uPlot> {
  loader ??= import('uplot').then((m) => (UPlotRef = m.default));
  return loader;
}

function cssVar(el: Element, name: string): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

export function buildOptions(el: HTMLElement, props: ChartProps, width: number): uPlot.Options {
  const ink = cssVar(el, '--ink');
  const mute = cssVar(el, '--mute');
  const acc = cssVar(el, '--acc');
  // Axes use the meta type (13px, 600).
  const font = `600 13px ${cssVar(el, '--font') || 'sans-serif'}`;
  const axis = (values?: uPlot.Axis['values'], y?: boolean): uPlot.Axis => ({
    stroke: mute,
    font,
    grid: { stroke: mute, width: 1 },
    ticks: { stroke: mute, width: 1, size: 4 },
    ...(values ? { values } : {}),
    // The y axis fits its widest label ("55.9 GB" was clipped to "5.9 GB" at the default 50 px).
    ...(y
      ? {
          size: (_u: uPlot, labels: string[] | null) => {
            measure ??= document.createElement('canvas').getContext('2d');
            if (!measure) return 50;
            measure.font = font;
            return Math.ceil(Math.max(24, ...(labels ?? []).map((l) => measure!.measureText(String(l ?? '')).width))) + 14;
          },
        }
      : {}),
  });
  const yFormat = props.yFormat;
  return {
    width,
    height: props.height ?? 180,
    legend: { show: false },
    cursor: { y: false, points: { show: false } },
    // uPlot widens a one-sample time axis by 86400/ms seconds (about 1,000 days); show a day either side.
    scales: { x: { time: props.time !== false, range: (u, a, b) => (u.data[0].length == 1 ? [a - 86400, a + 86400] : [a, b]) },...(props.yRange ? { y: { range: props.yRange } } : {}) },
    axes: [axis(), axis(yFormat ? (_u, splits) => splits.map((v) => (v === null ? '' : yFormat(v))) : undefined, true)],
    series: [
      {},
      ...props.series.map((s) => {
        const colour = s.tone === 'mute' ? mute : s.tone === 'acc' || (s.primary && !s.tone) ? acc : ink;
        return {
        label: s.label,
        stroke: colour,
        width: s.primary ? 2 : 1,
        ...(s.dashed ? { dash: [4, 4] } : {}),
        ...(props.bars ? { paths: barsPath(), fill: colour } : {}),
        points: { show: false },
        };
      }),
    ],
  };
}

/** uPlot wrapper: 1px --mute grid, --ink lines, --acc primary, meta-type axes; rebuilt on theme change. */
export function Chart(props: ChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  // uPlot loads asynchronously: build it from the props current at load time, not at effect time,
  // or data that arrived while the chunk was loading would never be drawn.
  const latest = useRef(props);
  latest.current = props;
  const [failed, setFailed] = useState(false);
  const theme = effectiveTheme.value;
  const seriesKey = props.series.map((s) => `${s.label}:${s.primary ? 1 : 0}:${s.dashed ? 1 : 0}:${s.tone ?? ''}`).join('|');

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let cancelled = false;
    let observer: ResizeObserver | null = null;
    loadUPlot()
      .then((UPlot) => {
        if (cancelled) return;
        const width = Math.max(200, el.clientWidth);
        const now = latest.current;
        plot.current = new UPlot(buildOptions(el, now, width), now.data, el);
        if (typeof ResizeObserver !== 'undefined') {
          observer = new ResizeObserver(([entry]) => {
            const w = Math.max(200, Math.floor(entry?.contentRect.width ?? width));
            plot.current?.setSize({ width: w, height: latest.current.height ?? 180 });
          });
          observer.observe(el);
        }
      })
      .catch(() => setFailed(true));
    return () => {
      cancelled = true;
      observer?.disconnect();
      plot.current?.destroy();
      plot.current = null;
    };
    // Rebuild only when the shape or theme changes; data updates go through setData.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, seriesKey, props.height, props.bars, props.time, String(props.yRange)]);

  useEffect(() => {
    plot.current?.setData(props.data);
  }, [props.data]);

  const xs = props.data[0] ?? [];
  const hasData = xs.length > 0 && props.data.slice(1).some((col) => (col ?? []).some((v) => v !== null && v !== undefined));
  const fmt = props.valueFormat ?? props.yFormat ?? ((v: number) => String(Math.round(v * 100) / 100));
  const lastRows = Array.from({ length: Math.min(10, xs.length) }, (_, k) => xs.length - Math.min(10, xs.length) + k);
  return (
    <figure class="chart" style={{ margin: 0 }}>
      {props.title && (
        <figcaption class="chart-title">
          <span class="label">{props.title}</span>
          {props.summary && <span class="meta tnum">{props.summary}</span>}
        </figcaption>
      )}
      {failed ? (
        <p class="meta">Chart unavailable</p>
      ) : (
        <div class="chart-host">
          <div ref={host} role="img" aria-label={props.ariaLabel ?? props.summary ?? props.title ?? 'Chart'} style={{ minHeight: `${props.height ?? 180}px` }} />
          {!hasData && props.empty && <p class="chart-empty meta">{props.empty}</p>}
        </div>
      )}
      {hasData && (
        <table class="visually-hidden">
          <caption>{props.title ?? props.ariaLabel ?? 'Chart'} — last samples</caption>
          <thead>
            <tr>
              <th scope="col">{props.days ? 'Day' : 'Time'}</th>
              {props.series.map((s) => (
                <th key={s.label} scope="col">
                  {s.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {lastRows.map((i) => (
              <tr key={i}>
                <td>{xLabel(xs[i] ?? 0, props)}</td>
                {props.series.map((s, si) => {
                  let v = props.data[si + 1]?.[i];
                  const below = props.stacked ? props.data[si + 2]?.[i] : null;
                  if (typeof v === 'number' && typeof below === 'number') v -= below;
                  return <td key={s.label}>{v === null || v === undefined ? '—' : fmt(v)}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </figure>
  );
}
