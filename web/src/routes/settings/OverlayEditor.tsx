/**
 * Sampling defaults and profile overlays (docs/ui/03 §5.2–5.3, SPEC §7.5): every field has a
 * Set / unset toggle so "no default" is distinct from a value (decision M7).
 */
import { useState } from 'preact/hooks';
import { NumberInput, Select, TagList, TextArea } from '../../components/inputs';
import { Toggle } from '../../components/Toggle';
import { t } from '../../strings/settings';
import { overlayFieldError, parseJsonObject } from './validate';

export type OverlayKind = 'number' | 'int' | 'effort' | 'priority' | 'stop' | 'json' | 'bool';

export const OVERLAY_FIELDS: ReadonlyArray<{ key: string; label: string; kind: OverlayKind; step?: number; note?: string }> = [
  { key: 'temperature', label: 'Temperature', kind: 'number', step: 0.05, note: 'all shapes' },
  { key: 'top_p', label: 'Top-p', kind: 'number', step: 0.01, note: 'all shapes' },
  { key: 'top_k', label: 'Top-k', kind: 'int', note: 'all shapes' },
  { key: 'min_p', label: 'Min-p', kind: 'number', step: 0.01, note: 'not Messages' },
  { key: 'presence_penalty', label: 'Presence penalty', kind: 'number', step: 0.1, note: 'not Messages' },
  { key: 'frequency_penalty', label: 'Frequency penalty', kind: 'number', step: 0.1, note: 'not Messages' },
  { key: 'repetition_penalty', label: 'Repetition penalty', kind: 'number', step: 0.01, note: 'not Messages' },
  { key: 'max_tokens', label: 'Max tokens', kind: 'int', note: 'max_completion_tokens / max_tokens / max_output_tokens; never on Messages' },
  { key: 'reasoning_effort', label: 'Reasoning effort', kind: 'effort', note: 'reasoning_effort / reasoning.effort' },
  { key: 'seed', label: 'Seed', kind: 'int' },
  { key: 'stop', label: 'Stop sequences', kind: 'stop' },
  { key: 'priority', label: 'Priority', kind: 'priority' },
  { key: 'timeout', label: 'Timeout', kind: 'number' },
  { key: 'chat_template_kwargs', label: 'chat_template_kwargs', kind: 'json', note: 'Chat only' },
  { key: 'ignore_eos', label: 'ignore_eos', kind: 'bool', note: 'Chat/Completions only' },
];

const EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
const PRIORITIES = ['foreground', 'normal', 'background'];

export function overlayErrors(overlay: Record<string, unknown>, jsonDrafts: Record<string, string> = {}): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of OVERLAY_FIELDS) {
    if (f.kind === 'json' && jsonDrafts[f.key] !== undefined) {
      const r = parseJsonObject(jsonDrafts[f.key]!, f.label);
      if (!r.ok) out[f.key] = r.error;
      continue;
    }
    const v = overlay[f.key];
    if (v === undefined || v === null) continue;
    const e = overlayFieldError(f.key, f.label, v);
    if (e) out[f.key] = e;
  }
  return out;
}

function defaultFor(kind: OverlayKind): unknown {
  switch (kind) {
    case 'effort':
      return 'none';
    case 'priority':
      return 'normal';
    case 'stop':
      return [];
    case 'json':
      return { enable_thinking: false };
    case 'bool':
      return true;
    default:
      return 0;
  }
}

export function OverlayEditor({ value, onChange, idPrefix }: { value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void; idPrefix: string }) {
  const [json, setJson] = useState<Record<string, string>>(() =>
    Object.fromEntries(OVERLAY_FIELDS.filter((f) => f.kind === 'json' && value[f.key] != null).map((f) => [f.key, JSON.stringify(value[f.key], null, 2)])),
  );
  const errors = overlayErrors(value, json);
  const set = (key: string, v: unknown) => {
    const next = { ...value };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(next);
  };
  return (
    <div class="stack overlay-editor">
      {OVERLAY_FIELDS.map((f) => {
        const isSet = value[f.key] !== undefined && value[f.key] !== null;
        const id = `${idPrefix}-${f.key}`;
        const err = errors[f.key];
        return (
          <div class="field" key={f.key} data-invalid={err ? 'true' : undefined} data-key={f.key}>
            <div class="field-head cluster">
              <Toggle
                checked={isSet}
                label={`${f.label} · ${isSet ? t('settings.value.on') : t('settings.value.unset')}`}
                onChange={(on) => {
                  if (!on) {
                    set(f.key, undefined);
                    const { [f.key]: _, ...rest } = json;
                    setJson(rest);
                  } else {
                    const d = defaultFor(f.kind);
                    set(f.key, d);
                    if (f.kind === 'json') setJson({ ...json, [f.key]: JSON.stringify(d, null, 2) });
                  }
                }}
              />
              <label class="label" for={id}>
                {f.label}
              </label>
              <span class="meta flag mono">{f.key}</span>
              {f.note && <span class="meta">{f.note}</span>}
            </div>
            {isSet && (
              <div class="field-body">
                {(f.kind === 'number' || f.kind === 'int') && (
                  <NumberInput
                    id={id}
                    value={typeof value[f.key] === 'number' ? (value[f.key] as number) : null}
                    step={f.kind === 'int' ? 1 : (f.step ?? 'any')}
                    invalid={!!err}
                    onChange={(n) => set(f.key, n === null ? 0 : n)}
                  />
                )}
                {f.kind === 'effort' && <Select id={id} value={String(value[f.key])} options={EFFORTS.map((e) => ({ value: e, label: e }))} onChange={(v) => set(f.key, v)} />}
                {f.kind === 'priority' && <Select id={id} value={String(value[f.key])} options={PRIORITIES.map((e) => ({ value: e, label: e }))} onChange={(v) => set(f.key, v)} />}
                {f.kind === 'stop' && (
                  <TagList
                    id={id}
                    label={f.label}
                    values={Array.isArray(value[f.key]) ? (value[f.key] as string[]) : typeof value[f.key] === 'string' ? [value[f.key] as string] : []}
                    onChange={(v) => set(f.key, v)}
                  />
                )}
                {f.kind === 'json' && (
                  <TextArea
                    id={id}
                    class="mono"
                    rows={3}
                    value={json[f.key] ?? ''}
                    invalid={!!err}
                    onChange={(text) => {
                      setJson({ ...json, [f.key]: text });
                      const r = parseJsonObject(text, f.label);
                      if (r.ok) set(f.key, r.value);
                    }}
                  />
                )}
                {f.kind === 'bool' && <Toggle id={id} checked={value[f.key] === true} onChange={(v) => set(f.key, v)} label={f.label} />}
              </div>
            )}
            {err && <p class="field-error">{err}</p>}
          </div>
        );
      })}
    </div>
  );
}
