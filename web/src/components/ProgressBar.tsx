export interface ProgressBarProps {
  /** 0..1, or null for indeterminate. */
  value: number | null;
  label: string;
  /** Accent fill for an active transfer; ink otherwise. */
  live?: boolean;
  valueText?: string;
}

export function ProgressBar({ value, label, live, valueText }: ProgressBarProps) {
  const clamped = value === null ? null : Math.min(1, Math.max(0, value));
  return (
    <div
      class="progress"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={clamped === null ? undefined : Math.round(clamped * 100)}
      aria-valuetext={valueText}
      data-live={String(Boolean(live))}
      data-indeterminate={String(clamped === null)}
    >
      <div class="progress-fill" style={{ width: `${(clamped ?? 0) * 100}%` }} />
    </div>
  );
}
