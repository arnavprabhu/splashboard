import type { ComponentChildren } from 'preact';
import { useEffect, useRef } from 'preact/hooks';
import { t } from '../strings/en';

// ---------- SegmentedControl ----------

export interface Segment<V extends string> {
  value: V;
  label: ComponentChildren;
  disabled?: boolean;
}

export interface SegmentedControlProps<V extends string> {
  value: V;
  options: readonly Segment<V>[];
  onChange: (value: V) => void;
  /** Accessible name of the group. */
  label: string;
  id?: string;
  size?: 'm' | 's';
}

/** Toggle generalised to N cells; the selected cell is ink-filled. ←/→ move the selection. */
export function SegmentedControl<V extends string>({ value, options, onChange, label, id, size = 'm' }: SegmentedControlProps<V>) {
  const enabled = options.filter((o) => !o.disabled);
  const move = (dir: 1 | -1) => {
    const i = enabled.findIndex((o) => o.value === value);
    const next = enabled[(i + dir + enabled.length) % enabled.length];
    if (next) onChange(next.value);
  };
  const group = useRef<HTMLDivElement>(null);
  return (
    <div
      ref={group}
      id={id}
      class="segmented"
      data-size={size}
      role="radiogroup"
      aria-label={label}
      onKeyDown={(e) => {
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
          e.preventDefault();
          move(1);
          requestAnimationFrame(() => group.current?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus());
        } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
          e.preventDefault();
          move(-1);
          requestAnimationFrame(() => group.current?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus());
        }
      }}
    >
      {options.map((o) => {
        const selected = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected ? 0 : -1}
            disabled={o.disabled}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ---------- Checkbox ----------

export interface CheckboxProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: ComponentChildren;
  id?: string;
  disabled?: boolean;
  /** Accessible label when `label` is visually hidden or not text. */
  ariaLabel?: string;
}

/** Square box with a ✕-free check glyph; native input underneath for a11y. */
export function Checkbox({ checked, onChange, label, id, disabled, ariaLabel }: CheckboxProps) {
  return (
    <label class="checkbox" data-disabled={disabled ? 'true' : undefined}>
      <input
        type="checkbox"
        id={id}
        checked={checked}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={(e) => onChange(e.currentTarget.checked)}
      />
      <span class="checkbox-box" aria-hidden="true">
        {checked ? '■' : ''}
      </span>
      <span class="checkbox-label">{label}</span>
    </label>
  );
}

// ---------- Slider (always paired with a number input by the caller) ----------

export interface SliderProps {
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
  label: string;
  id?: string;
  disabled?: boolean;
  valueText?: string;
  /** Tick marks with labels, e.g. a RAM marker. */
  ticks?: ReadonlyArray<{ value: number; label: string }>;
}

export function Slider({ value, min, max, step = 1, onChange, label, id, disabled, valueText, ticks }: SliderProps) {
  const pct = (v: number) => (max > min ? ((v - min) / (max - min)) * 100 : 0);
  return (
    <div class="slider">
      <input
        type="range"
        id={id}
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        aria-label={label}
        aria-valuetext={valueText}
        onInput={(e) => onChange(Number(e.currentTarget.value))}
      />
      {ticks && ticks.length > 0 && (
        <div class="slider-ticks" aria-hidden="true">
          {ticks.map((tk) => (
            <span key={tk.label} class="slider-tick meta" style={{ left: `${pct(tk.value)}%` }}>
              {tk.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------- SearchInput ----------

export interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  /** Accessible name, e.g. "Search models". */
  label: string;
  placeholder?: string;
  id?: string;
  /** Marks the page's primary search, focused by the `/` shortcut. */
  primary?: boolean;
  onSubmit?: () => void;
  autoFocus?: boolean;
}

/** Input with a SEARCH label prefix (no magnifier icon) and a CLEAR text button. Esc clears. */
export function SearchInput({ value, onChange, label, placeholder, id, primary, onSubmit, autoFocus }: SearchInputProps) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (autoFocus) ref.current?.focus();
  }, [autoFocus]);
  return (
    <div class="search">
      <span class="label search-prefix" aria-hidden="true">
        {t('common.search')}
      </span>
      <input
        ref={ref}
        id={id}
        type="search"
        role="searchbox"
        class="input"
        value={value}
        placeholder={placeholder}
        aria-label={label}
        data-primary-search={primary ? 'true' : undefined}
        spellcheck={false}
        autocomplete="off"
        onInput={(e) => onChange(e.currentTarget.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && value) {
            e.stopPropagation();
            onChange('');
          } else if (e.key === 'Enter' && onSubmit) {
            e.preventDefault();
            onSubmit();
          }
        }}
      />
      {value && (
        <button
          type="button"
          class="btn"
          data-variant="text"
          data-size="s"
          onClick={() => {
            onChange('');
            ref.current?.focus();
          }}
        >
          {t('common.clear')}
        </button>
      )}
    </div>
  );
}
