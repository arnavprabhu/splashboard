import type { ComponentChildren } from 'preact';
import { DASH } from '../lib/format';

export interface KeyValueItem {
  key: string;
  label: ComponentChildren;
  value: ComponentChildren;
  /** Muted line under the value (source field, reason for "—"). */
  meta?: ComponentChildren;
  /** Accent value: only for things that need attention (failures > 0). */
  accent?: boolean;
}

/** `<dl>` as 1px-ruled rows: label left, value right. Missing values show "—". */
export function KeyValue({ items, label }: { items: readonly KeyValueItem[]; label?: string }) {
  return (
    <dl class="kv" aria-label={label}>
      {items.map((item) => (
        <div class="kv-row" key={item.key}>
          <dt class="label">{item.label}</dt>
          <dd class="kv-value tnum">
            <span data-accent={item.accent ? 'true' : undefined} class={item.accent ? 'acc' : undefined}>
              {item.value === null || item.value === undefined || item.value === '' ? DASH : item.value}
            </span>
            {item.meta && <span class="meta kv-meta">{item.meta}</span>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export interface MeterBarProps {
  /** Segments drawn left to right as fractions of `total` (ink, then hatched, then outlined). */
  segments: ReadonlyArray<{ label: string; value: number | null | undefined }>;
  total: number | null | undefined;
  /** Optional marker, e.g. the memory limit. */
  marker?: { label: string; value: number } | undefined;
  label: string;
  valueText?: string;
}

/**
 * A horizontal stacked bar with a 1px rule box (memory, disk). Series are told apart by fill
 * (solid ink, hatched ink, none), never a third colour.
 */
export function MeterBar({ segments, total, marker, label, valueText }: MeterBarProps) {
  const sum = total && total > 0 ? total : null;
  return (
    <div class="meter" role="img" aria-label={valueText ? `${label}: ${valueText}` : label}>
      {sum !== null &&
        segments.map((s, i) => {
          const w = s.value && s.value > 0 ? Math.min(100, (s.value / sum) * 100) : 0;
          return <span key={s.label} class="meter-seg" data-kind={String(i % 3)} style={{ width: `${w}%` }} title={s.label} />;
        })}
      {sum !== null && marker && (
        <span class="meter-marker" style={{ left: `${Math.min(100, (marker.value / sum) * 100)}%` }} title={marker.label} />
      )}
    </div>
  );
}
