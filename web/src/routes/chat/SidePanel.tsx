/**
 * The side panel (docs/ui/07 §9): SAMPLING · SYSTEM · TOOLS · OUTPUT. Every field shows where
 * its effective value comes from (Profile / Model default / Splash default / Set here).
 */
import { Link } from 'wouter-preact';
import { Banner } from '../../components/Banner';
import { Button } from '../../components/Button';
import { Checkbox } from '../../components/controls';
import { Select, TagList, TextArea, TextInput } from '../../components/inputs';
import { Tag } from '../../components/Tag';
import { Toggle } from '../../components/Toggle';
import { Tooltip } from '../../components/Tooltip';
import { t } from '../../strings/chat';
import {
  MAX_STOP,
  NUM_FIELDS,
  OUTPUT_TEMPLATES,
  PRIORITIES,
  REASONING_EFFORTS,
  TOOL_TEMPLATES,
  type NumField,
  type OutputForm,
  type SamplingErrors,
  type SamplingForm,
  type ToolChoiceKind,
} from './logic';

export type PanelTab = 'sampling' | 'system' | 'tools' | 'output';
export const PANEL_TABS: readonly PanelTab[] = ['sampling', 'system', 'tools', 'output'];

/** Splash's own defaults (SPEC §3.4) for the placeholder chain. */
const SPLASH_DEFAULTS: Partial<Record<NumField, number>> = {
  temperature: 1,
  top_p: 0.95,
  top_k: 20,
  min_p: 0,
  presence_penalty: 0,
  frequency_penalty: 0,
  repetition_penalty: 1,
};

const STEP: Partial<Record<NumField, string>> = {
  temperature: '0.05',
  top_p: '0.01',
  min_p: '0.01',
  presence_penalty: '0.1',
  frequency_penalty: '0.1',
  repetition_penalty: '0.01',
};

const LABEL: Record<NumField, string> = {
  temperature: 'chat.sampling.temperature',
  top_p: 'chat.sampling.top_p',
  top_k: 'chat.sampling.top_k',
  min_p: 'chat.sampling.min_p',
  presence_penalty: 'chat.sampling.presence_penalty',
  frequency_penalty: 'chat.sampling.frequency_penalty',
  repetition_penalty: 'chat.sampling.repetition_penalty',
  seed: 'chat.sampling.seed',
  max_completion_tokens: 'chat.sampling.max_tokens',
  timeout: 'chat.sampling.timeout',
};

const HELP: Partial<Record<NumField, string>> = {
  temperature: 'chat.help.temperature',
  top_p: 'chat.help.top_p',
  top_k: 'chat.help.top_k',
  min_p: 'chat.help.min_p',
  presence_penalty: 'chat.help.penalty',
  frequency_penalty: 'chat.help.penalty',
  repetition_penalty: 'chat.help.repetition',
  seed: 'chat.help.seed',
  max_completion_tokens: 'chat.help.max_tokens',
  timeout: 'chat.help.timeout',
};

export interface SidePanelProps {
  tab: PanelTab;
  onTab: (tab: PanelTab) => void;
  // sampling
  sampling: SamplingForm;
  onSampling: (f: SamplingForm) => void;
  errors: SamplingErrors;
  profile: string;
  profiles: string[];
  onProfile: (p: string) => void;
  /** Overlay values of the picked profile and the model's own sampling defaults. */
  profileOverlay: Record<string, unknown>;
  modelDefaults: Record<string, unknown>;
  model: string | null;
  constrained: boolean;
  onReset: () => void;
  undo: (() => void) | null;
  // system
  system: string;
  onSystem: (s: string) => void;
  laterSystem: 'native' | 'patched' | 'unsupported' | null;
  onInsertSystem: () => void;
  // tools
  toolsText: string;
  onToolsText: (s: string) => void;
  toolsError: string | null;
  toolNames: string[];
  choice: ToolChoiceKind;
  onChoice: (c: ToolChoiceKind) => void;
  named: string;
  onNamed: (n: string) => void;
  choiceError: string | null;
  parallel: boolean;
  onParallel: (v: boolean) => void;
  mode: 'manual' | 'mcp';
  onMode: (m: 'manual' | 'mcp') => void;
  servers: Array<{ name: string; tools: number; enabled: boolean; alwaysAllow: boolean; on: boolean; error: string | null }>;
  onServerOn: (name: string, on: boolean) => void;
  onAlwaysAllow: (name: string, on: boolean) => void;
  collisions: Array<{ name: string; server: string }>;
  toolsPending: boolean;
  // output
  output: OutputForm;
  onOutput: (f: OutputForm) => void;
  outputError: string | null;
}

function numStr(v: unknown): string | null {
  return typeof v === 'number' && Number.isFinite(v) ? String(v) : null;
}

function SourceTag({ source }: { source: 'profile' | 'model_default' | 'splash_default' | 'set_here' }) {
  return <Tag tone={source === 'set_here' ? 'ink' : 'mute'}>{t(`chat.source.${source}`)}</Tag>;
}

function SamplingTab(p: SidePanelProps) {
  const f = p.sampling;
  const set = (patch: Partial<SamplingForm>) => p.onSampling({ ...f, ...patch });
  const setNum = (k: NumField, v: string) => p.onSampling({ ...f, num: { ...f.num, [k]: v } });
  const effective = (k: NumField): { value: string | null; source: 'profile' | 'model_default' | 'splash_default' } => {
    const key = k === 'max_completion_tokens' ? 'max_tokens' : k;
    const prof = numStr(p.profileOverlay[key]);
    if (prof !== null) return { value: prof, source: 'profile' };
    const md = numStr(p.modelDefaults[key]);
    if (md !== null) return { value: md, source: 'model_default' };
    const sd = SPLASH_DEFAULTS[k];
    return { value: sd === undefined ? null : String(sd), source: sd === undefined ? 'model_default' : 'splash_default' };
  };
  return (
    <div class="stack chat-panel-fields">
      <div class="field">
        <label class="label" for="chat-profile">
          {t('chat.sampling.profile')}
        </label>
        <Select
          id="chat-profile"
          value={p.profile}
          options={(p.profiles.length ? p.profiles : ['default']).map((x) => ({ value: x, label: x }))}
          onChange={p.onProfile}
        />
        <p class="field-help">{t('chat.sampling.profile_help')}</p>
        {p.model && (
          <Link href={`/models/${p.model}/settings#profiles`} class="meta">
            {t('chat.sampling.edit_profiles')}
          </Link>
        )}
        <div class="cluster">
          <Button size="s" variant="text" onClick={p.onReset}>
            {t('chat.sampling.reset')}
          </Button>
          {p.undo && (
            <Button size="s" variant="text" onClick={p.undo}>
              {t('chat.sampling.undo_reset')}
            </Button>
          )}
        </div>
      </div>
      <div class="field">
        <div class="field-head cluster">
          <label class="label" for="chat-effort">
            {t('chat.sampling.reasoning_effort')}
          </label>
          <span class="meta flag mono">reasoning_effort</span>
          <SourceTag source={f.reasoning_effort ? 'set_here' : typeof p.profileOverlay.reasoning_effort === 'string' ? 'profile' : 'model_default'} />
        </div>
        <Select
          id="chat-effort"
          value={f.reasoning_effort}
          options={[{ value: '', label: t('chat.sampling.model_default') }, ...REASONING_EFFORTS.map((e) => ({ value: e, label: e }))]}
          onChange={(v) => set({ reasoning_effort: v })}
        />
        <p class="field-help">{t('chat.help.reasoning_effort')}</p>
      </div>
      {NUM_FIELDS.map((k) => {
        const eff = effective(k);
        const label = t(LABEL[k] as 'chat.sampling.temperature');
        const err = p.errors.num[k];
        const setHere = f.num[k].trim() !== '';
        return (
          <div class="field" key={k} data-invalid={err ? 'true' : undefined} data-field={k}>
            <div class="field-head cluster">
              <label class="label" for={`chat-${k}`}>
                {label}
              </label>
              <span class="meta flag mono">{k}</span>
              <SourceTag source={setHere ? 'set_here' : eff.source} />
            </div>
            <div class="cluster chat-num">
              <input
                id={`chat-${k}`}
                class="input"
                type="number"
                inputMode="decimal"
                step={STEP[k] ?? '1'}
                value={f.num[k]}
                placeholder={eff.value ?? t('chat.sampling.model_default')}
                aria-invalid={err ? 'true' : undefined}
                aria-describedby={err ? `chat-${k}-err` : undefined}
                onInput={(e) => setNum(k, e.currentTarget.value)}
              />
              {setHere && (
                <button type="button" class="btn" data-variant="text" data-size="s" aria-label={t('chat.field.clear', { label })} onClick={() => setNum(k, '')}>
                  ×
                </button>
              )}
            </div>
            {HELP[k] && <p class="field-help">{t(HELP[k] as 'chat.help.seed')}</p>}
            {err && (
              <p class="field-error" id={`chat-${k}-err`} role="alert">
                {err}
              </p>
            )}
          </div>
        );
      })}
      <div class="field" data-invalid={p.errors.stop ? 'true' : undefined}>
        <div class="field-head cluster">
          <span class="label">{t('chat.sampling.stop')}</span>
          <span class="meta flag mono">stop</span>
          <SourceTag source={f.stop.length ? 'set_here' : 'model_default'} />
        </div>
        {p.constrained && f.stop.length > 0 && <p class="meta">{t('chat.sampling.ignore_eos_disabled')}</p>}
        <TagList
          label={t('chat.sampling.stop')}
          values={f.stop}
          placeholder={t('chat.sampling.stop_placeholder')}
          onChange={(v) => set({ stop: v.slice(0, MAX_STOP + 1) })}
        />
        <p class="field-help">{t('chat.help.stop')}</p>
        {p.errors.stop && <p class="field-error">{p.errors.stop}</p>}
      </div>
      <div class="field">
        <div class="field-head cluster">
          <label class="label" for="chat-priority">
            {t('chat.sampling.priority')}
          </label>
          <span class="meta flag mono">priority</span>
        </div>
        <Select
          id="chat-priority"
          value={f.priority || 'normal'}
          options={PRIORITIES.map((x) => ({ value: x, label: t(`chat.sampling.priority.${x}`) }))}
          onChange={(v) => set({ priority: v === 'normal' ? '' : v })}
        />
        <p class="field-help">{t('chat.help.priority')}</p>
      </div>
      <div class="field">
        <div class="field-head cluster">
          <span class="label">{t('chat.sampling.ignore_eos')}</span>
          <span class="meta flag mono">ignore_eos</span>
        </div>
        {p.constrained ? (
          <Tooltip text={t('chat.sampling.ignore_eos_disabled')}>
            <span tabIndex={0}>
              <Toggle checked={false} disabled onChange={() => undefined} label={t('chat.sampling.ignore_eos')} />
            </span>
          </Tooltip>
        ) : (
          <Toggle checked={f.ignore_eos} onChange={(v) => set({ ignore_eos: v })} label={t('chat.sampling.ignore_eos')} />
        )}
        <p class="field-help">{t('chat.help.ignore_eos')}</p>
      </div>
      <div class="field" data-invalid={p.errors.kwargs ? 'true' : undefined}>
        <div class="field-head cluster">
          <label class="label" for="chat-kwargs">
            {t('chat.sampling.kwargs')}
          </label>
          <span class="meta flag mono">chat_template_kwargs</span>
        </div>
        <TextArea id="chat-kwargs" class="mono" rows={4} value={f.kwargs} onChange={(v) => set({ kwargs: v })} />
        <div class="cluster">
          <Button
            size="s"
            variant="text"
            onClick={() => {
              try {
                set({ kwargs: JSON.stringify(JSON.parse(f.kwargs), null, 2) });
              } catch {
                /* leave as typed */
              }
            }}
          >
            {t('chat.sampling.kwargs_format')}
          </Button>
          <Button size="s" variant="text" onClick={() => set({ kwargs: '{ "enable_thinking": false }' })}>
            <span class="mono">{t('chat.sampling.kwargs_template')}</span>
          </Button>
        </div>
        <p class="field-help">{t('chat.help.kwargs')}</p>
        {p.errors.kwargs && <p class="field-error">{p.errors.kwargs}</p>}
      </div>
    </div>
  );
}

function SystemTab(p: SidePanelProps) {
  return (
    <div class="stack chat-panel-fields">
      <div class="field">
        <label class="label" for="chat-system">
          {t('chat.system.prompt')}
        </label>
        <TextArea id="chat-system" rows={6} value={p.system} onChange={p.onSystem} />
        <p class="field-help">{t('chat.system.prompt_help')}</p>
      </div>
      <div class="field">
        <span class="label">{t('chat.system.later')}</span>
        {p.laterSystem === 'unsupported' && (
          <Banner tone="warn" title={t('chat.system.unsupported_title')}>
            {t('chat.system.unsupported', { msg: "this model's chat template does not accept system messages after the first message" })}
          </Banner>
        )}
        {p.laterSystem === 'patched' && <p class="meta">{t('chat.system.patched')}</p>}
        <Button size="s" variant="text" disabled={p.laterSystem === 'unsupported'} onClick={p.onInsertSystem}>
          {t('chat.system.insert')}
        </Button>
      </div>
    </div>
  );
}

function ToolsTab(p: SidePanelProps) {
  return (
    <div class="stack chat-panel-fields">
      <div class="field" data-invalid={p.toolsError ? 'true' : undefined}>
        <div class="field-head cluster">
          <label class="label" for="chat-tools">
            {t('chat.tools.definitions')}
          </label>
          <span class="meta tnum">{t('chat.tools.defined', { n: p.toolNames.length })}</span>
        </div>
        <TextArea id="chat-tools" class="mono" rows={8} value={p.toolsText} onChange={p.onToolsText} spellcheck={false} />
        <div class="cluster">
          <Button
            size="s"
            variant="text"
            onClick={() => {
              try {
                p.onToolsText(JSON.stringify(JSON.parse(p.toolsText || '[]'), null, 2));
              } catch {
                /* the error line explains */
              }
            }}
          >
            {t('chat.sampling.kwargs_format')}
          </Button>
          <Select
            aria-label={t('chat.tools.template')}
            value=""
            options={[
              { value: '', label: `${t('chat.tools.template')} ▾` },
              ...(['get_weather', 'search', 'run_sql', 'empty'] as const).map((k) => ({ value: k, label: t(`chat.tools.template.${k}`) })),
            ]}
            onChange={(v) => v && p.onToolsText(JSON.stringify(TOOL_TEMPLATES[v as keyof typeof TOOL_TEMPLATES], null, 2))}
          />
        </div>
        <p class="field-help">{t('chat.tools.help')}</p>
        {p.toolsError && <p class="field-error">{p.toolsError}</p>}
        {p.toolNames.length > 0 && (
          <p class="cluster chat-tool-tags">
            {p.toolNames.map((n) => (
              <Tag key={n}>
                <span class="mono">{n}</span>
              </Tag>
            ))}
          </p>
        )}
      </div>
      <div class="field" data-invalid={p.choiceError ? 'true' : undefined}>
        <label class="label" for="chat-choice">
          {t('chat.tools.choice')}
        </label>
        <div class="cluster">
          <Select
            id="chat-choice"
            value={p.choice}
            options={(['auto', 'none', 'required', 'named'] as const).map((c) => ({ value: c, label: t(`chat.tools.choice.${c}`) }))}
            onChange={(v) => p.onChoice(v as ToolChoiceKind)}
          />
          {p.choice === 'named' && (
            <Select
              aria-label={t('chat.tools.choice_named')}
              value={p.named}
              options={p.toolNames.map((n) => ({ value: n, label: n }))}
              onChange={p.onNamed}
            />
          )}
        </div>
        {p.choiceError && <p class="field-error">{p.choiceError}</p>}
      </div>
      <div class="field">
        <span class="label">{t('chat.tools.parallel')}</span>
        <Toggle checked={p.parallel} onChange={p.onParallel} label={t('chat.tools.parallel')} />
        <span class="meta flag mono">parallel_tool_calls</span>
      </div>
      <fieldset class="field chat-exec">
        <legend class="label">{t('chat.tools.execution')}</legend>
        {(['manual', 'mcp'] as const).map((m) => (
          <label key={m} class="chat-radio">
            <input type="radio" name="chat-exec" checked={p.mode === m} onChange={() => p.onMode(m)} />
            <span>
              {t(`chat.tools.exec.${m}`)} <span class="meta">— {t(`chat.tools.exec_help.${m}`)}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <div class="field">
        <div class="field-head cluster">
          <span class="label">{t('chat.tools.mcp_servers')}</span>
          <Link href="/settings/chat" class="meta">
            {t('chat.tools.configure')}
          </Link>
        </div>
        {p.servers.length === 0 ? (
          <p class="meta">{t('chat.tools.no_servers')}</p>
        ) : (
          <ul class="chat-servers">
            {p.servers.map((s) => (
              <li key={s.name} class="chat-server">
                <span class="mono">{s.name}</span>
                <span class="meta">{s.error ? s.error : t('chat.tools.server_tools', { n: s.tools })}</span>
                <Toggle checked={s.on} onChange={(v) => p.onServerOn(s.name, v)} label={t('chat.tools.server_on', { server: s.name })} />
                <Checkbox
                  checked={s.alwaysAllow}
                  onChange={(v) => p.onAlwaysAllow(s.name, v)}
                  label={t('chat.tools.always_allow')}
                  ariaLabel={t('chat.tools.always_allow_label', { server: s.name })}
                />
              </li>
            ))}
          </ul>
        )}
        {p.collisions.map((c) => (
          <p key={c.name} class="field-error">
            {t('chat.tools.collision', { name: c.name, server: c.server })}
          </p>
        ))}
      </div>
    </div>
  );
}

function OutputTab(p: SidePanelProps) {
  const f = p.output;
  const set = (patch: Partial<OutputForm>) => p.onOutput({ ...f, ...patch });
  return (
    <div class="stack chat-panel-fields">
      <fieldset class="field">
        <legend class="label">{t('chat.output.format')}</legend>
        {(['text', 'json_object', 'json_schema'] as const).map((k) => (
          <label key={k} class="chat-radio">
            <input type="radio" name="chat-format" checked={f.kind === k} onChange={() => set({ kind: k })} />
            <span>{t(`chat.output.${k}`)}</span>
          </label>
        ))}
      </fieldset>
      {f.kind === 'json_schema' && (
        <>
          <div class="field" data-invalid={p.outputError ? 'true' : undefined}>
            <div class="field-head cluster">
              <label class="label" for="chat-schema">
                {t('chat.output.schema')}
              </label>
              <span class={p.outputError ? 'label acc' : 'label'}>{p.outputError ? t('chat.output.invalid') : t('chat.output.valid')}</span>
            </div>
            <TextArea id="chat-schema" class="mono" rows={10} value={f.schema} onChange={(v) => set({ schema: v })} spellcheck={false} />
            <div class="cluster">
              <Button
                size="s"
                variant="text"
                onClick={() => {
                  try {
                    set({ schema: JSON.stringify(JSON.parse(f.schema), null, 2) });
                  } catch {
                    /* error shown */
                  }
                }}
              >
                {t('chat.sampling.kwargs_format')}
              </Button>
              <Select
                aria-label={t('chat.output.template')}
                value=""
                options={[
                  { value: '', label: `${t('chat.output.template')} ▾` },
                  ...(['person', 'list', 'classification'] as const).map((k) => ({ value: k, label: t(`chat.output.template.${k}`) })),
                ]}
                onChange={(v) => v && set({ schema: JSON.stringify(OUTPUT_TEMPLATES[v as keyof typeof OUTPUT_TEMPLATES], null, 2) })}
              />
            </div>
            {p.outputError && <p class="field-error">{p.outputError}</p>}
          </div>
          <div class="field cluster">
            <label class="label" for="chat-schema-name">
              {t('chat.output.name')}
            </label>
            <TextInput id="chat-schema-name" class="mono" value={f.name} onChange={(v) => set({ name: v })} />
            <span class="label">{t('chat.output.strict')}</span>
            <Toggle checked={f.strict} onChange={(v) => set({ strict: v })} label={t('chat.output.strict')} />
          </div>
        </>
      )}
      {f.kind !== 'text' && <p class="field-help">{t('chat.output.help')}</p>}
    </div>
  );
}

export function SidePanel(p: SidePanelProps) {
  const attention: Record<PanelTab, boolean> = {
    sampling: Object.keys(p.errors.num).length > 0 || !!p.errors.stop || !!p.errors.kwargs,
    system: false,
    tools: !!p.toolsError || !!p.choiceError || p.toolsPending,
    output: !!p.outputError,
  };
  return (
    <section class="chat-panel" aria-label={t('chat.panel.label')}>
      <div class="chat-panel-tabs" role="tablist" aria-label={t('chat.panel.tabs')}>
        {PANEL_TABS.map((tab) => {
          const name = t(`chat.panel.tab.${tab}`);
          return (
            <button
              key={tab}
              type="button"
              role="tab"
              id={`chat-tab-${tab}`}
              aria-selected={p.tab === tab}
              aria-controls="chat-tabpanel"
              tabIndex={p.tab === tab ? 0 : -1}
              class="navlink nav"
              aria-label={attention[tab] ? t('chat.panel.tab_attention', { tab: name }) : undefined}
              onClick={() => p.onTab(tab)}
              onKeyDown={(e) => {
                if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
                e.preventDefault();
                const i = PANEL_TABS.indexOf(p.tab);
                const next = PANEL_TABS[(i + (e.key === 'ArrowRight' ? 1 : PANEL_TABS.length - 1)) % PANEL_TABS.length]!;
                p.onTab(next);
                requestAnimationFrame(() => document.getElementById(`chat-tab-${next}`)?.focus());
              }}
            >
              {name}
              {attention[tab] && (
                <span class="acc" aria-hidden="true">
                  {' '}●
                </span>
              )}
            </button>
          );
        })}
      </div>
      <div id="chat-tabpanel" role="tabpanel" aria-labelledby={`chat-tab-${p.tab}`}>
        {p.tab === 'sampling' && <SamplingTab {...p} />}
        {p.tab === 'system' && <SystemTab {...p} />}
        {p.tab === 'tools' && <ToolsTab {...p} />}
        {p.tab === 'output' && <OutputTab {...p} />}
      </div>
    </section>
  );
}
