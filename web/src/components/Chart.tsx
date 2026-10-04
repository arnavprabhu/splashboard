import { useEffect, useRef, useState } from 'preact/hooks';
import type uPlot from 'uplot';
import { effectiveTheme } from '../store/theme';

export interface ChartSeries {
  label: string;
  /** The primary series is drawn in --acc; the rest in --ink. */
  primary?: boolean;
  /** Dashed ink line for secondary references such as p95 or a limit. */
  dashed?: boolean;
}

export interface ChartProps {
  title?: string;
  /** x values (unix seconds) first, then one array per series. */
  data: uPlot.AlignedData;
  series: readonly ChartSeries[];
  height?: number;
  yFormat?: (value: number) => string;
  /** Accessible summary of the latest values. */
  summary?: string;
}

let loader: Promise<typeof uPlot> | null = null;
/** uPlot is code-split and only fetched by pages that draw a chart. */
export function loadUPlot(): Promise<typeof uPlot> {
  loader ??= import('uplot').then((m) => m.default);
  return loader;
}

function cssVar(el: Element, name: string): string {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

function buildOptions(el: HTMLElement, props: ChartProps, width: number): uPlot.Options {
  const ink = cssVar(el, '--ink');
  const mute = cssVar(el, '--mute');
  const acc = cssVar(el, '--acc');
  const font = `600 12px ${cssVar(el, '--font') || 'sans-serif'}`;
  const axis = (values?: uPlot.Axis['values']): uPlot.Axis => ({
    stroke: mute,
    font,
    grid: { stroke: mute, width: 1 },
    ticks: { stroke: mute, width: 1, size: 4 },
    ...(values ? { values } : {}),
  });
  const yFormat = props.yFormat;
  return {
    width,
    height: props.height ?? 180,
    legend: { show: false },
    cursor: { y: false, points: { show: false } },
    scales: { x: { time: true } },
    axes: [axis(), axis(yFormat ? (_u, splits) => splits.map((v) => (v === null ? '' : yFormat(v))) : undefined)],
    series: [
      {},
      ...props.series.map((s) => ({
        label: s.label,
        stroke: s.primary ? acc : ink,
        width: s.primary ? 2 : 1,
        ...(s.dashed ? { dash: [4, 4] } : {}),
        points: { show: false },
      })),
    ],
  };
}

/** uPlot wrapper: 1px --mute grid, --ink lines, --acc primary, meta-type axes; rebuilt on theme change. */
export function Chart(props: ChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const [failed, setFailed] = useState(false);
  const theme = effectiveTheme.value;
  const seriesKey = props.series.map((s) => `${s.label}:${s.primary ? 1 : 0}:${s.dashed ? 1 : 0}`).join('|');

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    let cancelled = false;
    let observer: ResizeObserver | null = null;
    loadUPlot()
      .then((UPlot) => {
        if (cancelled) return;
        const width = Math.max(200, el.clientWidth);
        plot.current = new UPlot(buildOptions(el, props, width), props.data, el);
        if (typeof ResizeObserver !== 'undefined') {
          observer = new ResizeObserver(([entry]) => {
            const w = Math.max(200, Math.floor(entry?.contentRect.width ?? width));
            plot.current?.setSize({ width: w, height: props.height ?? 180 });
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
  }, [theme, seriesKey, props.height]);

  useEffect(() => {
    plot.current?.setData(props.data);
  }, [props.data]);

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
        <div ref={host} role="img" aria-label={props.summary ?? props.title ?? 'Chart'} style={{ minHeight: `${props.height ?? 180}px` }} />
      )}
    </figure>
  );
}
