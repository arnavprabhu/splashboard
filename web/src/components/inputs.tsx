import type { PartialInputHTMLAttributes, SelectHTMLAttributes, TextareaHTMLAttributes } from 'preact';
import { useState } from 'preact/hooks';
import { formatBytes } from '../lib/format';
import { parseSize, type SizeKind } from '../lib/size';

type BaseInput = Omit<PartialInputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'onInput' | 'type' | 'class' | 'role'> & {
  class?: string;
};

export interface TextInputProps extends BaseInput {
  value: string;
  onChange: (value: string) => void;
  invalid?: boolean;
  type?: 'text' | 'url' | 'password' | 'search' | 'email';
}

export function TextInput({ value, onChange, invalid, type = 'text', class: cls, ...rest }: TextInputProps) {
  return (
    <input
      {...rest}
      // Preact's input typings discriminate on a single literal `type`.
      type={type as 'text'}
      class={['input', cls].filter(Boolean).join(' ')}
      value={value}
      aria-invalid={invalid ? 'true' : undefined}
      onInput={(e) => onChange(e.currentTarget.value)}
    />
  );
}

export interface NumberInputProps extends Omit<BaseInput, 'min' | 'max' | 'step'> {
  value: number | null;
  onChange: (value: number | null) => void;
  min?: number;
  max?: number;
  step?: number | 'any';
  invalid?: boolean;
}

/** Empty input reports null, so "unset / use default" is representable. */
export function NumberInput({ value, onChange, min, max, step = 'any', invalid, class: cls, ...rest }: NumberInputProps) {
  const outOfRange = value !== null && ((min !== undefined && value < min) || (max !== undefined && value > max));
  return (
    <input
      {...rest}
      type="number"
      inputMode="decimal"
      class={['input', cls].filter(Boolean).join(' ')}
      value={value === null ? '' : String(value)}
      min={min}
      max={max}
      step={step}
      aria-invalid={invalid || outOfRange ? 'true' : undefined}
      onInput={(e) => {
        const raw = e.currentTarget.value.trim();
        if (raw === '') return onChange(null);
        const n = Number(raw);
        if (Number.isFinite(n)) onChange(n);
      }}
    />
  );
}

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'value' | 'onChange' | 'onInput' | 'class' | 'role'> {
  class?: string;
  value: string;
  options: readonly SelectOption[];
  onChange: (value: string) => void;
}

export function Select({ value, options, onChange, class: cls, ...rest }: SelectProps) {
  return (
    <span class={['select', cls].filter(Boolean).join(' ')}>
      <select {...rest} value={value} onChange={(e) => onChange(e.currentTarget.value)}>
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </select>
    </span>
  );
}

export interface TextAreaProps extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'value' | 'onChange' | 'onInput' | 'class' | 'role'> {
  class?: string;
  value: string;
  onChange: (value: string) => void;
  invalid?: boolean;
}

export function TextArea({ value, onChange, invalid, class: cls, ...rest }: TextAreaProps) {
  return (
    <textarea
      {...rest}
      class={['input', cls].filter(Boolean).join(' ')}
      value={value}
      aria-invalid={invalid ? 'true' : undefined}
      onInput={(e) => onChange(e.currentTarget.value)}
    />
  );
}

export interface TagListProps {
  values: readonly string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  /** Returns an error message, or null when the value is acceptable. */
  validate?: (value: string) => string | null;
  label: string;
  id?: string;
}

/** Editable list of strings (allowed hosts/origins, served model names). Enter or comma adds. */
export function TagList({ values, onChange, placeholder, validate, label, id }: TagListProps) {
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const add = () => {
    const value = draft.trim();
    if (!value) return;
    if (values.includes(value)) {
      setError('Already in the list');
      return;
    }
    const problem = validate?.(value) ?? null;
    if (problem) {
      setError(problem);
      return;
    }
    onChange([...values, value]);
    setDraft('');
    setError(null);
  };
  return (
    <div class="stack" style={{ gap: '8px' }}>
      <div class="taglist">
        {values.map((v) => (
          <span class="taglist-item" key={v}>
            <span>{v}</span>
            <button type="button" aria-label={`Remove ${v}`} onClick={() => onChange(values.filter((x) => x !== v))}>
              ×
            </button>
          </span>
        ))}
        <input
          id={id}
          class="input"
          value={draft}
          placeholder={placeholder}
          aria-label={label}
          aria-invalid={error ? 'true' : undefined}
          onInput={(e) => {
            setDraft(e.currentTarget.value);
            setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ',') {
              e.preventDefault();
              add();
            } else if (e.key === 'Backspace' && draft === '' && values.length > 0) {
              onChange(values.slice(0, -1));
            }
          }}
          onBlur={add}
        />
      </div>
      {error && (
        <p class="field-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export interface SizeInputProps extends Omit<TextInputProps, 'invalid'> {
  kind: SizeKind;
}

/** Byte-size field validated like Splash's parsers (K/M/G = 1024). Shows the parsed size. */
export function SizeInput({ kind, value, onChange, id, ...rest }: SizeInputProps) {
  const result = parseSize(kind, value);
  const hintId = id ? `${id}-size` : undefined;
  return (
    <div class="stack" style={{ gap: '6px' }}>
      <TextInput
        {...rest}
        id={id}
        value={value}
        onChange={onChange}
        invalid={!result.ok}
        spellcheck={false}
        autocomplete="off"
        aria-describedby={hintId}
      />
      <p id={hintId} class={result.ok ? 'meta tnum' : 'field-error'}>
        {result.ok
          ? result.bytes === null
            ? 'Automatic'
            : result.bytes === 0
              ? 'Disabled'
              : `= ${formatBytes(result.bytes, { digits: 2 })}`
          : result.error}
      </p>
    </div>
  );
}
