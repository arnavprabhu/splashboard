import { signal } from '@preact/signals';
import { useEffect, useMemo, useState } from 'preact/hooks';
import { useLocation } from 'wouter-preact';
import type { InstalledModel } from '../../api/models';
import type { EngineSummary } from '../../api/types';
import { Button } from '../../components/Button';
import { CodeBlock } from '../../components/CodeBlock';
import { ConfirmSheet } from '../../components/ConfirmSheet';
import { Disclosure } from '../../components/Disclosure';
import { Menu, type MenuItem } from '../../components/Menu';
import { StatusChip } from '../../components/StatusChip';
import { DASH, formatBytes, formatDuration, formatPercent, formatRelativeTime, formatTokens, formatTokPerSec } from '../../lib/format';
import { liveSample } from '../../store/live';
import { t } from '../../strings/status';
import { useEngineCalls, type EngineCall } from './actions';
import { formatLabel, headerActions, metaKeys, needsConfirm, shortName, stateGroup, viewOf, type HeaderAction } from './logic';

/** The startup output of the current engine session, kept after Ready. */
export const startupCapture = signal<{ key: string; lines: string[]; command: string | null; template: string | null } | null>(null);

export function sessionKey(e: EngineSummary | null): string {
  const v = viewOf(e?.view);
  return `${e?.model ?? ''}|${v.started_at ?? ''}`;
}

function useUptime(e: EngineSummary | null, ticking: boolean): number | null {
  const [now, setNow] = useState(() => Date.now());
  const base = useMemo(() => ({ s: e?.uptime_s ?? null, at: Date.now() }), [e]);
  useEffect(() => {
    if (base.s === null || !ticking) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [base, ticking]);
  return base.s === null ? null : base.s + (ticking ? Math.max(0, (now - base.at) / 1000) : 0);
}

const DOWNLOADING = new Set(['downloading', 'paused', 'verifying']);

export function modelMenuItems(models: readonly InstalledModel[] | null, active: string | null, pick: (id: string) => void): MenuItem[] {
  return (models ?? []).map((m) => {
    const busy = DOWNLOADING.has(m.status);
    const detail = busy
      ? t('status.actions.downloading', { pct: formatPercent(m.progress ?? null, 0) })
      : [formatLabel(m), formatBytes(m.size_bytes), m.last_used_at ? formatRelativeTime(m.last_used_at) : null].filter(Boolean).join(' · ');
    return {
      key: m.id,
      label: <span class="mono">{m.id}</span>,
      text: m.id,
      detail,
      checked: m.id === active,
      disabled: busy,
      onSelect: () => pick(m.id),
    };
  });
}

interface Pending {
  action: 'stop' | 'restart' | 'switch';
  model?: string;
}

export interface HeaderProps {
  engine: EngineSummary | null;
  models: readonly InstalledModel[] | null;
  modelsFailed: boolean;
  persistentCache: boolean;
}

/** Header band: model name, chip, meta, actions per state. */
export function StatusHeader({ engine: e, models, modelsFailed, persistentCache }: HeaderProps) {
  const [, navigate] = useLocation();
  const state = e?.state ?? null;
  const group = stateGroup(state);
  const { pending, run } = useEngineCalls();
  const [confirm, setConfirm] = useState<Pending | null>(null);
  const uptime = useUptime(e, group === 'live');
  const model = e?.model ?? null;
  const installed = models?.find((m) => m.id === model) ?? null;
  const showModel = group !== 'stopped' && group !== 'unknown' && model;
  const title = showModel ? t('status.title_model', { name: shortName(model) }) : t('status.title_stopped');
  const sample = liveSample.value;
  const tokps = state === 'busy' ? sample?.throughput?.decode_tps : null;
  const inFlight = e?.requests_in_flight ?? 0;
  const hasModels = models ? models.length > 0 : modelsFailed ? false : null;

  const meta = {
    format: formatLabel(installed),
    context: e?.maximum_context_tokens ?? null,
    kv: e?.kv_format ?? null,
    vision: e?.vision ?? null,
    draft: showModel ? (e?.draft ?? (installed?.draft?.repo_id ? installed.draft.repo_id : null)) : undefined,
    uptime: group === 'live' && uptime !== null ? formatDuration(uptime) : null,
    version: e?.engine_version ?? null,
    starting: group === 'starting',
  };
  const metaText: Record<string, string> = {
    format: meta.format ?? '',
    context: t('status.meta.context', { v: meta.context != null ? formatTokens(meta.context) : DASH }),
    kv: t('status.meta.kv', { v: meta.kv ?? '' }),
    vision: t(meta.vision ? 'status.meta.vision_on' : 'status.meta.vision_off'),
    draft: meta.draft ? t('status.meta.draft', { v: meta.draft }) : t('status.meta.draft_none'),
    uptime: t('status.meta.uptime', { v: meta.uptime ?? '' }),
    version: t('status.meta.version', { v: meta.version ?? '' }),
  };
  const items = showModel ? metaKeys(meta) : meta.version ? (['version'] as const) : [];

  const doCall = (call: EngineCall, force = false) => run(call, force);
  const request = (action: HeaderAction, target?: string) => {
    if (action === 'open_downloader') return navigate('/models/downloader');
    if (action === 'retry' && model) return doCall({ kind: 'load', model });
    const kind = action === 'switch' ? 'switch' : action === 'stop' ? 'stop' : action === 'restart' ? 'restart' : null;
    if (action === 'load' && target) return doCall({ kind: 'load', model: target });
    if (!kind) return;
    if (needsConfirm(action, inFlight)) {
      setConfirm(target ? { action: kind, model: target } : { action: kind });
      return;
    }
    if (kind === 'switch' && target) doCall({ kind: 'load', model: target, switching: true });
    else if (kind === 'stop') doCall({ kind: 'stop' });
    else if (kind === 'restart') doCall({ kind: 'restart' });
  };

  const onConfirm = () => {
    if (!confirm) return;
    const c = confirm;
    setConfirm(null);
    if (c.action === 'switch' && c.model) doCall({ kind: 'load', model: c.model, switching: true }, true);
    else if (c.action === 'stop') doCall({ kind: 'stop' }, true);
    else doCall({ kind: 'restart' }, true);
  };

  const openModels: MenuItem = { key: '__open_models', label: t('status.actions.open_models'), onSelect: () => navigate('/models') };
  const menuGroups = (pick: (id: string) => void) => {
    const list = modelMenuItems(models, group === 'stopped' ? null : model, pick);
    const placeholder: MenuItem[] =
      list.length === 0 ? [{ key: '__none', label: t(modelsFailed ? 'status.actions.models_unavailable' : 'status.actions.no_models'), disabled: true }] : [];
    return [{ items: [...list, ...placeholder] }, { items: [openModels] }];
  };

  const capture = startupCapture.value;
  const showCapture = (group === 'live' || group === 'frozen') && capture && capture.key === sessionKey(e) && (capture.lines.length > 0 || capture.command);

  return (
    <header class="page-head stack status-head" data-testid="status-header">
      <h1 class="page-title display-l">{title}</h1>
      <div class="status-id">
        {showModel ? <span class="mono status-id-model">{model}</span> : <span class="body">{t('status.no_model')}</span>}
        <StatusChip state={state} announce={false} />
        {tokps != null && <span class="label tnum">{t('status.tokps', { v: formatTokPerSec(tokps, { unit: false }) })}</span>}
      </div>
      {(items.length > 0 || state === 'idle_released' || (state === 'stopping' && persistentCache)) && (
        <p class="meta tnum status-meta" aria-label={t('status.meta.label')}>
          {[...items.map((k) => metaText[k]), state === 'idle_released' ? t('status.meta.released') : null, state === 'stopping' && persistentCache ? t('status.meta.flushing') : null]
            .filter(Boolean)
            .join(' · ')}
        </p>
      )}
      <div class="cluster status-actions" role="group" aria-label={t('status.actions.label')}>
        {headerActions(state, hasModels).map((a) => {
          if (a.id === 'switch' || a.id === 'load') {
            const pick = (id: string) => request(a.id, id);
            return (
              <Menu
                key={a.id}
                label={t(a.id === 'switch' ? 'status.actions.switch' : 'status.actions.load')}
                variant={a.accent ? 'accent' : 'outline'}
                disabled={a.disabled || pending !== null}
                groups={menuGroups(pick)}
                radio={a.id === 'switch'}
                testId={`status-${a.id}`}
              />
            );
          }
          const label =
            a.id === 'restart' && state === 'engine_failed'
              ? t('status.actions.restart_engine')
              : t(
                  a.id === 'stop'
                    ? 'status.actions.stop'
                    : a.id === 'restart'
                      ? 'status.actions.restart'
                      : a.id === 'retry'
                        ? 'status.actions.retry'
                        : 'status.actions.open_downloader',
                );
          const key = a.id === 'retry' ? `load:${model ?? ''}` : a.id;
          return (
            <Button
              key={a.id}
              variant={a.accent ? 'accent' : 'outline'}
              disabled={a.disabled || (pending !== null && pending !== key)}
              loading={pending === key}
              onClick={() => request(a.id)}
              data-testid={`status-${a.id}`}
            >
              {label}
            </Button>
          );
        })}
      </div>
      {showCapture && capture && (
        <Disclosure summary={t('status.startup_log')}>
          <div class="stack" style={{ gap: '12px' }}>
            {capture.template && <p class="meta">{t('status.startup_log.template', { mode: capture.template })}</p>}
            {capture.command && <CodeBlock code={capture.command} label={t('status.startup_log.command')} what={t('status.startup_log.command')} wrap />}
            {capture.lines.length > 0 && <CodeBlock code={capture.lines.join('\n')} copy={false} maxHeight={260} />}
          </div>
        </Disclosure>
      )}
      <ConfirmSheet
        open={confirm !== null}
        title={t(confirm?.action === 'stop' ? 'status.confirm.stop_title' : confirm?.action === 'restart' ? 'status.confirm.restart_title' : 'status.confirm.switch_title')}
        confirmLabel={t(confirm?.action === 'stop' ? 'status.confirm.stop' : confirm?.action === 'restart' ? 'status.confirm.restart' : 'status.confirm.switch')}
        important
        onConfirm={onConfirm}
        onClose={() => setConfirm(null)}
      >
        <ul class="stack" style={{ gap: '8px', margin: 0, paddingLeft: '1.1em' }}>
          <li class="body tnum">{t('status.confirm.running', { n: inFlight })}</li>
          {confirm?.model && (
            <li class="body">
              {t('status.confirm.switch_to')} <span class="mono">{confirm.model}</span>
            </li>
          )}
        </ul>
      </ConfirmSheet>
    </header>
  );
}

