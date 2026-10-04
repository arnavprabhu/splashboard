/**
 * One schema-driven settings field (docs/ui/05 §2): the shared `Field` anatomy plus the control
 * chosen by `SchemaField.control`, the source chip from GET /settings/effective, the
 * Restart / Next load / Rebind badge, the "?" disclosure with Splash's `--help` text verbatim,
 * Splash's validation wording, `!` warnings, the dirty rule and `#key` deep links.
 */

import type { ComponentChildren } from 'preact';
import { useState } from 'preact/hooks';
import type { SchemaField } from '../../api/models';
import {
  Button,
  Field,
  NumberInput,
  Select,
  SizeInput,
  Slider,
  Tag,
  TagList,
  TextInput,
  Toggle,
  type SettingSource,
} from '../../components';
import { formatBytes, formatPercent, formatTokens } from '../../lib/format';
import { parseMaxContext, parseMaxMemory, type SizeKind } from '../../lib/size';
import { t } from '../../strings/settings';
import { deepEqual, UNSET, type FieldRef } from './form';
import type { SettingsForm } from './state';
import { aliasError, hostError, originError } from './validate';
import { fieldHelp, fieldLabel, choiceLabel } from './labels';

export interface SettingFieldProps {
  field: SchemaField;
  form: SettingsForm;
  /** The per-model page passes the model; the global page passes null. */
  model?: string | null;
  engineVersion?: string | null;
  /** Disabled with a reason Tag (`Legacy package`, `Needs SSD cache`). */
  disabledReason?: string | null;
  /** A Tag shown without disabling (`Needs SSD cache` on a toggle that still works). */
  note?: string | null;
  /** Content under the control (size footnotes, endpoint lines, actions). */
  extra?: ComponentChildren;
  /** Replaces the default control. */
  control?: (ids: { id: string; describedBy: string }, value: unknown, set: (v: unknown) => void, disabled: boolean) => ComponentChildren;
  /** Installed model IDs for `control: "model"` (null while unknown). */
  models?: readonly string[] | null;
  /** Host RAM for the memory slider (bytes). */
  memoryBytes?: number | null;
  /** Placeholder for path fields (the resolved default). */
  placeholder?: string;
  /** Highlighted by a `#key` deep link. */
  highlight?: boolean;
}

const SIZE_KIND: Readonly<Record<string, SizeKind>> = {
  'serve.max_memory': 'max-memory',
  'serve.max_cache_disk': 'max-cache-disk',
  'serve.max_request_size': 'request-size',
};

/** Selects whose `null` value means "not set" (shown as the first option). */
const NULLABLE_SELECT: ReadonlySet<string> = new Set(['serve.default_reasoning_effort']);

const CONTEXT_TICKS = [4096, 8192, 16384, 32768, 65536, 131072, 262144];
const GiB = 1024 ** 3;

/** The source chip: saved values come from the effective view; edits compare with the default. */
export function sourceOf(form: SettingsForm, field: SchemaField, ref: FieldRef): SettingSource {
  const eff = form.effective.value?.values[field.key];
  const dirty = form.isDirty(ref);
  if (ref.model) {
    if (form.value(ref) !== undefined) return 'model';
    if (!dirty && eff) return eff.source === 'model' ? 'model' : eff.source;
    const globalValue = eff?.global_value ?? field.default;
    return deepEqual(globalValue, field.default) ? 'default' : 'global';
  }
  if (!dirty && eff) return eff.source === 'model' ? 'global' : eff.source;
  return deepEqual(form.value(ref), field.default) ? 'default' : 'global';
}

export function restartLabelOf(field: SchemaField, model: string | null | undefined): string | null {
  if (field.key === 'server.host' || field.key === 'server.port') return t('settings.badge.rebind');
  if (field.applies === 'restart') return model ? t('settings.badge.next_load') : t('settings.badge.restart');
  if (field.applies === 'next_load') return t('settings.badge.next_load');
  return null;
}

function flagText(field: SchemaField): string | undefined {
  if (field.flag) return field.flag;
  if (field.env) return `env ${field.env}`;
  return field.key;
}

/** The "?" disclosure: Splash's own help text verbatim, then our note. */
export function MoreText({ field, version }: { field: SchemaField; version?: string | null }) {
  const engineHelp = field.engine?.help;
  return (
    <div class="stack" style={{ gap: '8px' }}>
      {engineHelp && (
        <p>
          <span class="meta">{t('settings.more.splash', { version: version ?? '' })}</span>{' '}
          <q class="mono settings-quote">{engineHelp}</q>
        </p>
      )}
      {field.help && <p class="mute">{field.help}</p>}
      <p class="meta">
        <code class="mono">{field.key}</code>
        {field.env && field.flag ? (
          <>
            {' · '}
            <code class="mono">env {field.env}</code>
          </>
        ) : null}
      </p>
    </div>
  );
}

/** The share of this Mac's RAM, on the SizeInput's own "= 40 GB" line (docs/ui/05:
 * one line, `= 34.4 GB · 54% of 64 GB`). The parsed size itself is the SizeInput's. */
export function memoryShare(bytes: number, memoryBytes: number | null | undefined): string | null {
  if (!memoryBytes) return null;
  return t('settings.memory.footnote', { pct: formatPercent(bytes / memoryBytes, 0), ram: formatBytes(memoryBytes, { digits: 0 }) });
}

/** Auto + slider + text for `serve.max_context` (docs/ui/05 §3.4). */
function ContextControl({ id, describedBy, label, value, set, disabled }: { id: string; describedBy: string; label: string; value: unknown; set: (v: unknown) => void; disabled: boolean }) {
  const text = value === undefined || value === null ? 'auto' : String(value);
  const auto = text.trim().toLowerCase() === 'auto';
  const parsed = parseMaxContext(text);
  const tokens = parsed.ok && parsed.tokens ? parsed.tokens : 131072;
  const log = Math.log2(Math.min(262144, Math.max(4096, tokens)));
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <div class="stack settings-auto" style={{ gap: '10px' }}>
      <div class="cluster" style={{ gap: '12px', alignItems: 'center' }}>
        <span class="meta">{t('settings.auto')}</span>
        <Toggle
          checked={auto}
          label={t('settings.auto_for', { label })}
          disabled={disabled}
          onChange={(on) => set(on ? 'auto' : (draft ?? '128K'))}
        />
      </div>
      {!auto && (
        <>
          <Slider
            label={label}
            min={12}
            max={18}
            step={0.5}
            value={log}
            disabled={disabled}
            valueText={formatTokens(tokens)}
            ticks={CONTEXT_TICKS.map((v) => ({ value: Math.log2(v), label: formatTokens(v) }))}
            onChange={(v) => {
              const n = Math.round(2 ** v / 1024) * 1024;
              const k = `${Math.round(n / 1024)}K`;
              setDraft(k);
              set(k);
            }}
          />
          <TextInput
            id={id}
            aria-describedby={describedBy}
            class="mono settings-short"
            value={text}
            disabled={disabled}
            spellcheck={false}
            autocomplete="off"
            onChange={(v) => {
              setDraft(v);
              set(v);
            }}
          />
          {parsed.ok && parsed.tokens !== null && <p class="meta tnum">{t('settings.context.footnote', { tokens: parsed.tokens.toLocaleString('en-US') })}</p>}
        </>
      )}
      {auto && <p class="meta">{t('settings.context.auto_note')}</p>}
    </div>
  );
}

/** Auto + slider (GB) + size input for `serve.max_memory`. */
function MemoryControl({ id, describedBy, label, value, set, disabled, memoryBytes }: { id: string; describedBy: string; label: string; value: unknown; set: (v: unknown) => void; disabled: boolean; memoryBytes: number | null | undefined }) {
  const text = value === undefined || value === null ? 'auto' : String(value);
  const auto = text.trim().toLowerCase() === 'auto';
  const ramGb = memoryBytes ? Math.round(memoryBytes / GiB) : 64;
  const parsed = parseMaxMemory(text);
  const gb = parsed.ok && parsed.bytes ? Math.round(parsed.bytes / GiB) : Math.max(4, ramGb - 8);
  return (
    <div class="stack settings-auto" style={{ gap: '10px' }}>
      <div class="cluster" style={{ gap: '12px', alignItems: 'center' }}>
        <span class="meta">{t('settings.auto')}</span>
        <Toggle checked={auto} label={t('settings.auto_for', { label })} disabled={disabled} onChange={(on) => set(on ? 'auto' : `${gb}G`)} />
      </div>
      {!auto && (
        <>
          <Slider
            label={label}
            min={4}
            max={Math.max(8, ramGb)}
            step={1}
            value={Math.min(Math.max(4, gb), Math.max(8, ramGb))}
            disabled={disabled}
            valueText={`${gb} GB`}
            ticks={[
              { value: 4, label: '4 GB' },
              { value: Math.max(8, ramGb), label: t('settings.memory.ram_tick', { ram: `${ramGb} GB` }) },
            ]}
            onChange={(v) => set(`${v}G`)}
          />
          <SizeInput
            id={id}
            aria-describedby={describedBy}
            kind="max-memory"
            class="mono settings-short"
            value={text}
            disabled={disabled}
            suffix={(bytes) => memoryShare(bytes, memoryBytes)}
            onChange={(v) => set(v)}
          />
        </>
      )}
      {auto && <p class="meta">{t('settings.memory.auto_note')}</p>}
    </div>
  );
}

/** SSD cache: Toggle (0 ↔ a size, default 16G) + size input (docs/ui/05 §3.5). */
function CacheDiskControl({ id, describedBy, value, set, disabled }: { id: string; describedBy: string; value: unknown; set: (v: unknown) => void; disabled: boolean }) {
  const text = value === undefined || value === null ? '0' : String(value);
  const on = text.trim() !== '0';
  const [last, setLast] = useState(on ? text : '16G');
  return (
    <div class="stack" style={{ gap: '10px' }}>
      <Toggle
        checked={on}
        label={t('settings.field.serve.max_cache_disk.label')}
        disabled={disabled}
        onChange={(next) => {
          if (on) setLast(text);
          set(next ? last || '16G' : '0');
        }}
      />
      {on && <SizeInput id={id} aria-describedby={describedBy} kind="max-cache-disk" class="mono settings-short" value={text} disabled={disabled} onChange={(v) => set(v)} />}
    </div>
  );
}

/** Off / seconds for `serve.request_timeout` (duration). */
function DurationControl({ id, describedBy, value, set, disabled, unit }: { id: string; describedBy: string; value: unknown; set: (v: unknown) => void; disabled: boolean; unit: string }) {
  const on = typeof value === 'number';
  return (
    <div class="cluster" style={{ gap: '12px', alignItems: 'center' }}>
      <Toggle checked={on} label={t('settings.duration.toggle')} disabled={disabled} onChange={(next) => set(next ? 3600 : null)} />
      {on && (
        <>
          <NumberInput id={id} aria-describedby={describedBy} class="settings-short tnum" value={value} min={0} disabled={disabled} onChange={(v) => set(v)} />
          <span class="meta">{unit}</span>
        </>
      )}
    </div>
  );
}

function tagValidator(key: string): ((v: string) => string | null) | undefined {
  if (key === 'server.allowed_origins') return originError;
  if (key === 'server.allowed_hosts') return hostError;
  if (key === 'serve.served_model_names') return aliasError;
  return undefined;
}

export function SettingField(props: SettingFieldProps) {
  const { field, form, model = null, engineVersion, disabledReason, note, extra, control, models, memoryBytes, placeholder, highlight } = props;
  const ref: FieldRef = { key: field.key, model };
  const value = form.value(ref);
  const set = (v: unknown) => form.set(ref, v);
  const disabled = Boolean(disabledReason) || Boolean(form.envelope.value?.read_only);
  const dirty = form.isDirty(ref);
  const errors = form.errorsFor(ref);
  const warnings = form.warningsFor(ref);
  const restart = restartLabelOf(field, model);
  const source = sourceOf(form, field, ref);
  const label = fieldLabel(field);
  const scopeNote = !model && field.scope === 'GM' ? t('settings.scope.gm_suffix') : null;
  const help = fieldHelp(field);

  // On the per-model page an unset override shows the inherited value.
  const shown = model && value === undefined ? form.effective.value?.values[field.key]?.global_value ?? field.default : value;

  const badges = (
    <>
      {disabledReason && <Tag tone="mute">{disabledReason}</Tag>}
      {note && !disabledReason && <Tag tone="mute">{note}</Tag>}
    </>
  );

  const renderControl = (ids: { id: string; describedBy: string }) => {
    if (control) return control(ids, shown, set, disabled);
    const common = { id: ids.id, 'aria-describedby': ids.describedBy || undefined, disabled };
    switch (field.control) {
      case 'toggle':
        return <Toggle id={ids.id} describedBy={ids.describedBy || undefined} label={label} checked={Boolean(shown)} disabled={disabled} onChange={(v) => set(v)} />;
      case 'number': {
        if (field.key === 'engine.internal_port') {
          const auto = shown === 'auto' || shown === undefined || shown === null;
          return (
            <div class="cluster" style={{ gap: '12px', alignItems: 'center' }}>
              <span class="meta">{t('settings.auto')}</span>
              <Toggle checked={auto} label={t('settings.auto_for', { label })} disabled={disabled} onChange={(on) => set(on ? 'auto' : 18000)} />
              {!auto && <NumberInput {...common} class="settings-short tnum" value={typeof shown === 'number' ? shown : null} min={field.min ?? undefined} max={field.max ?? undefined} step={1} onChange={(v) => set(v)} />}
            </div>
          );
        }
        if (field.key === 'downloads.parallel') {
          return (
            <Select
              {...common}
              value={String(shown ?? 1)}
              options={[1, 2, 3].map((n) => ({ value: String(n), label: String(n) }))}
              onChange={(v) => set(Number(v))}
            />
          );
        }
        return (
          <div class="cluster" style={{ gap: '12px', alignItems: 'center' }}>
            <NumberInput
              {...common}
              class="settings-short tnum"
              value={typeof shown === 'number' ? shown : null}
              min={field.min ?? undefined}
              max={field.max ?? undefined}
              step={field.key === 'serve.decode_share' ? 0.1 : 'any'}
              invalid={errors.length > 0}
              onChange={(v) => set(v)}
            />
            {field.unit && <span class="meta">{field.unit}</span>}
          </div>
        );
      }
      case 'duration':
        return <DurationControl id={ids.id} describedBy={ids.describedBy} value={shown} set={set} disabled={disabled} unit={field.unit ?? 's'} />;
      case 'select': {
        const choices = field.choices ?? [];
        const nullable = NULLABLE_SELECT.has(field.key);
        const options = [
          ...(nullable ? [{ value: '', label: t('settings.choice.model_default') }] : []),
          ...choices.map((c) => ({ value: c, label: choiceLabel(field.key, c) })),
        ];
        return <Select {...common} value={shown === null || shown === undefined ? '' : String(shown)} options={options} onChange={(v) => set(nullable && v === '' ? null : v)} />;
      }
      case 'size': {
        const kind = SIZE_KIND[field.key] ?? 'request-size';
        if (field.key === 'serve.max_memory')
          return <MemoryControl id={ids.id} describedBy={ids.describedBy} label={label} value={shown} set={set} disabled={disabled} memoryBytes={memoryBytes} />;
        if (field.key === 'serve.max_cache_disk') return <CacheDiskControl id={ids.id} describedBy={ids.describedBy} value={shown} set={set} disabled={disabled} />;
        return <SizeInput {...common} kind={kind} class="mono settings-short" value={String(shown ?? '')} onChange={(v) => set(v)} />;
      }
      case 'context':
        return <ContextControl id={ids.id} describedBy={ids.describedBy} label={label} value={shown} set={set} disabled={disabled} />;
      case 'tags': {
        const list = Array.isArray(shown) ? (shown as string[]) : [];
        return <TagList id={ids.id} label={label} values={list} validate={tagValidator(field.key)} onChange={(v) => set(v)} />;
      }
      case 'model': {
        const current = typeof shown === 'string' ? shown : '';
        if (models && models.length > 0) {
          const options = [{ value: '', label: t('settings.choice.none') }, ...models.map((m) => ({ value: m, label: m }))];
          if (current && !models.includes(current)) options.push({ value: current, label: t('settings.choice.not_installed', { model: current }) });
          return <Select {...common} class="mono" value={current} options={options} onChange={(v) => set(v === '' ? null : v)} />;
        }
        return (
          <TextInput {...common} class="mono" value={current} placeholder="owner/repo[:VARIANT]" spellcheck={false} autocomplete="off" onChange={(v) => set(v.trim() === '' ? null : v.trim())} />
        );
      }
      case 'path':
      case 'text':
      case 'url':
        return (
          <TextInput
            {...common}
            class={field.control === 'text' && !field.key.includes('revision') && !field.key.includes('draft') ? undefined : 'mono'}
            type={field.control === 'url' ? 'url' : 'text'}
            value={typeof shown === 'string' ? shown : ''}
            placeholder={placeholder}
            spellcheck={false}
            autocomplete="off"
            invalid={errors.length > 0}
            onChange={(v) => set(v.trim() === '' ? null : v)}
          />
        );
      default:
        return <code class="mono">{JSON.stringify(shown)}</code>;
    }
  };

  const reset =
    model && value !== undefined ? (
      <Button variant="text" size="s" onClick={() => form.set(ref, UNSET)} disabled={disabled}>
        {field.scope === 'GM' ? t('settings.model.reset_to_global') : t('settings.model.clear')}
      </Button>
    ) : null;

  const inheritedNote =
    model && value === undefined && field.scope === 'GM' ? (
      <span class="meta">{t('settings.model.inherits', { value: displayValue(shown) })}</span>
    ) : null;

  return (
    <div class="setting" data-key={field.key} id={field.key} data-dirty={dirty ? 'true' : undefined} data-highlight={highlight ? 'true' : undefined}>
      <Field
        label={label}
        help={
          help || scopeNote ? (
            <>
              {help}
              {scopeNote && <span> {scopeNote}</span>}
            </>
          ) : undefined
        }
        flag={flagText(field)}
        source={field.storage === 'keychain' ? undefined : source}
        restart={Boolean(restart)}
        restartLabel={restart ?? undefined}
        more={<MoreText field={field} version={engineVersion} />}
        error={errors[0] ?? null}
        warnings={warnings}
        badges={disabledReason || note ? badges : undefined}
      >
        {(ids) => (
          <>
            {renderControl(ids)}
            {(reset || inheritedNote) && (
              <div class="cluster" style={{ gap: '12px', alignItems: 'baseline' }}>
                {inheritedNote}
                {reset}
              </div>
            )}
            {extra}
          </>
        )}
      </Field>
    </div>
  );
}

export function displayValue(v: unknown): string {
  if (v === null || v === undefined) return t('settings.value.unset');
  if (typeof v === 'boolean') return v ? t('settings.value.on') : t('settings.value.off');
  if (Array.isArray(v)) return v.length === 0 ? t('settings.value.empty_list') : v.join(', ');
  return String(v);
}
