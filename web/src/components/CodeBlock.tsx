import type { ComponentChildren } from 'preact';
import { useMemo, useState } from 'preact/hooks';
import { t } from '../strings/en';
import { CopyButton } from './CopyButton';

export interface CodeBlockProps {
  code: string;
  /** What is being copied, for screen readers ("curl command"). */
  what?: string;
  copy?: boolean;
  /** Offer a wrap toggle (long lines scroll inside the block otherwise). */
  wrapToggle?: boolean;
  wrap?: boolean;
  label?: ComponentChildren;
  maxHeight?: number;
  children?: ComponentChildren;
}

/** `<pre class="mono">` in a 1px rule box with an optional COPY button. */
export function CodeBlock({ code, what, copy = true, wrapToggle, wrap: initialWrap = false, label, maxHeight, children }: CodeBlockProps) {
  const [wrap, setWrap] = useState(initialWrap);
  return (
    <div class="codeblock">
      {(label || copy || wrapToggle) && (
        <div class="codeblock-head">
          {label ? <span class="label">{label}</span> : <span />}
          <span class="cluster" style={{ gap: '12px' }}>
            {wrapToggle && (
              <button type="button" class="btn" data-variant="text" data-size="s" aria-pressed={wrap} onClick={() => setWrap(!wrap)}>
                {wrap ? t('common.nowrap') : t('common.wrap')}
              </button>
            )}
            {copy && <CopyButton text={code} what={what} />}
          </span>
        </div>
      )}
      <pre class="mono codeblock-pre" data-wrap={String(wrap)} tabIndex={0} style={maxHeight ? { maxHeight: `${maxHeight}px` } : undefined}>
        {children ?? code}
      </pre>
    </div>
  );
}

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? 'undefined';
  } catch {
    return String(value);
  }
}

export interface JsonViewProps {
  value: unknown;
  /** Expanded top-level groups by default (L4 for /status). */
  expandTop?: boolean;
  /** Top-level keys open by default; when given it replaces `expandTop` for depth 1. */
  openKeys?: readonly string[];
  /** Muted suffix after a leaf value, e.g. `55834574848 · 52.0 GB` for `*_bytes` keys. */
  annotate?: (key: string, value: unknown) => string | null;
  label?: ComponentChildren;
  what?: string;
  maxHeight?: number;
}

interface JsonOptions {
  expandTop: boolean;
  openKeys?: readonly string[] | undefined;
  annotate?: ((key: string, value: unknown) => string | null) | undefined;
}

/**
 * Collapsible JSON tree with a COPY of the whole document. Objects and arrays render as
 * native disclosures so keyboard and screen readers work without extra code.
 */
export function JsonView({ value, expandTop = true, openKeys, annotate, label, what = 'JSON', maxHeight }: JsonViewProps) {
  const text = useMemo(() => jsonText(value), [value]);
  const options: JsonOptions = { expandTop, openKeys, annotate };
  return (
    <div class="codeblock jsonview">
      <div class="codeblock-head">
        {label ? <span class="label">{label}</span> : <span />}
        <CopyButton text={text} what={what} />
      </div>
      <div class="mono jsonview-body" tabIndex={0} style={maxHeight ? { maxHeight: `${maxHeight}px` } : undefined}>
        <JsonNode value={value} depth={0} options={options} />
      </div>
    </div>
  );
}

function JsonNode({ value, depth, options, name }: { value: unknown; depth: number; options: JsonOptions; name?: string }) {
  const keyEl = name !== undefined ? <span class="json-key">{JSON.stringify(name)}: </span> : null;
  if (value === null || typeof value !== 'object') {
    const note = name !== undefined && options.annotate ? options.annotate(name, value) : null;
    return (
      <div class="json-leaf">
        {keyEl}
        <span class={`json-${value === null ? 'null' : typeof value}`}>{JSON.stringify(value) ?? 'undefined'}</span>
        {note && <span class="meta json-note"> · {note}</span>}
      </div>
    );
  }
  const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v] as const) : Object.entries(value as Record<string, unknown>);
  const open =
    depth === 0 || (depth === 1 && (options.openKeys ? name !== undefined && options.openKeys.includes(name) : options.expandTop));
  const brackets = Array.isArray(value) ? ['[', ']'] : ['{', '}'];
  if (entries.length === 0) {
    return (
      <div class="json-leaf">
        {keyEl}
        {brackets.join('')}
      </div>
    );
  }
  return (
    <details class="json-group" open={open}>
      <summary>
        {keyEl}
        {brackets[0]}
        <span class="meta"> {entries.length} </span>
      </summary>
      <div class="json-children">
        {entries.map(([k, v]) => (
          <JsonNode key={k} name={Array.isArray(value) ? undefined : k} value={v} depth={depth + 1} options={options} />
        ))}
      </div>
      <div>{brackets[1]}</div>
    </details>
  );
}
