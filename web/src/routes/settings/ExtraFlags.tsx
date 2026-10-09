/**
 * "Other engine options": `engine.extra_flags[]` as rows joined with
 * the options this Splash version has that Settings does not manage (`schema.engine_options.
 * unknown`). store_true flags are toggles, choices are selects, the rest text; free text via
 * "Add raw" for flags the schema does not list (marked Unverified).
 */
import { useState } from 'preact/hooks';
import type { EngineOptionOut } from '../../api/models';
import { Button } from '../../components/Button';
import { Disclosure } from '../../components/Disclosure';
import { Select, TextInput } from '../../components/inputs';
import { Table } from '../../components/Table';
import { Tag } from '../../components/Tag';
import { Toggle } from '../../components/Toggle';
import { t } from '../../strings/settings';
import type { SettingsForm } from './state';
import { flagNameError, flagsToArgv, parseRawFlags, type RawFlag } from './validate';

/** `["--a", "1", "--b"]` → rows; a value is the next token that is not a flag. */
export function argvToFlags(argv: readonly unknown[]): RawFlag[] {
  const out: RawFlag[] = [];
  for (const raw of argv) {
    const tok = String(raw);
    if (tok.startsWith('--')) out.push({ flag: tok, value: null });
    else if (out.length && out[out.length - 1]!.value === null) out[out.length - 1]!.value = tok;
  }
  return out;
}

export function ExtraFlags({ form, options, version }: { form: SettingsForm; options: readonly EngineOptionOut[]; version: string | null }) {
  const ref = { key: 'engine.extra_flags', model: form.model };
  const stored = form.value(ref);
  const rows = argvToFlags(Array.isArray(stored) ? stored : []);
  const [raw, setRaw] = useState('');
  const [rawError, setRawError] = useState<string | null>(null);
  const byFlag = new Map(options.map((o) => [o.flag, o]));
  const issues = form.errorsFor(ref);
  const set = (next: RawFlag[]) => form.set(ref, flagsToArgv(next));
  const unused = options.filter((o) => !rows.some((r) => r.flag === o.flag));
  return (
    <div class="field" data-key="engine.extra_flags" id="engine.extra_flags">
      <div class="field-head cluster">
        <span class="label">{t('settings.extra.title')}</span>
        <Tag tone="ink">{t('settings.badge.restart')}</Tag>
      </div>
      <p class="field-help">{t('settings.extra.count', { version: version ?? '—', n: options.length })}</p>
      {rows.length > 0 && (
        <Table
          caption={t('settings.extra.title')}
          rows={rows}
          rowKey={(r) => r.flag}
          columns={[
            {
              key: 'flag',
              label: t('settings.extra.flag'),
              render: (r) => (
                <span class="cluster">
                  <span class="mono">{r.flag}</span>
                  {!byFlag.has(r.flag) && <Tag tone="mute">{t('settings.extra.unverified')}</Tag>}
                </span>
              ),
            },
            {
              key: 'value',
              label: t('settings.extra.value'),
              render: (r) => {
                const opt = byFlag.get(r.flag);
                const update = (value: string | null) => set(rows.map((x) => (x.flag === r.flag ? { ...x, value } : x)));
                if (opt && !opt.takes_value) return <Toggle checked label={r.flag} onChange={(on) => !on && set(rows.filter((x) => x.flag !== r.flag))} />;
                if (opt?.choices?.length)
                  return <Select aria-label={r.flag} value={r.value ?? ''} options={opt.choices.map((c) => ({ value: String(c), label: String(c) }))} onChange={update} />;
                return <TextInput class="mono" aria-label={r.flag} value={r.value ?? ''} onChange={(v) => update(v || null)} />;
              },
            },
            { key: 'help', label: t('settings.extra.help'), render: (r) => <span class="meta">{byFlag.get(r.flag)?.help ? `“${byFlag.get(r.flag)!.help}”` : '—'}</span> },
            {
              key: 'remove',
              label: t('settings.extra.remove'),
              hideLabel: true,
              render: (r) => (
                <Button size="s" variant="text" onClick={() => set(rows.filter((x) => x.flag !== r.flag))}>
                  {t('settings.extra.remove')}
                </Button>
              ),
            },
          ]}
        />
      )}
      {issues.map((e) => (
        <p key={e} class="field-error">
          {e}
        </p>
      ))}
      <div class="cluster">
        {unused.length > 0 && (
          <Select
            aria-label={t('settings.extra.add')}
            value=""
            options={[{ value: '', label: `${t('settings.extra.add')} ▾` }, ...unused.map((o) => ({ value: o.flag, label: o.flag }))]}
            onChange={(flag) => {
              const opt = byFlag.get(flag);
              if (opt) set([...rows, { flag, value: opt.takes_value ? (opt.default != null ? String(opt.default) : '') : null }]);
            }}
          />
        )}
      </div>
      <Disclosure summary={t('settings.extra.raw')}>
        <div class="cluster">
          <TextInput class="mono" value={raw} placeholder="--new-thing 42" aria-label={t('settings.extra.raw')} onChange={setRaw} invalid={!!rawError} />
          <Button
            size="s"
            onClick={() => {
              const parsed = parseRawFlags(raw);
              if (!parsed.ok) return setRawError(parsed.error);
              const bad = parsed.flags.map((f) => flagNameError(f.flag)).find(Boolean);
              if (bad) return setRawError(bad);
              setRawError(null);
              setRaw('');
              set([...rows.filter((r) => !parsed.flags.some((f) => f.flag === r.flag)), ...parsed.flags]);
            }}
          >
            {t('settings.extra.add_raw')}
          </Button>
        </div>
        {rawError && <p class="field-error">{rawError}</p>}
      </Disclosure>
    </div>
  );
}
