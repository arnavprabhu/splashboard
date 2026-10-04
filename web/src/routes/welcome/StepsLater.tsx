/** Wizard steps 3–5 (docs/ui/04 §4–6): use case with the preset diff, first model, start. */
import { useEffect, useState } from 'preact/hooks';
import { useLocation } from 'wouter-preact';
import type { InstalledModels } from '../../api/models';
import { api } from '../../api/client';
import { Banner } from '../../components/Banner';
import { Button } from '../../components/Button';
import { CodeBlock } from '../../components/CodeBlock';
import { CopyButton } from '../../components/CopyButton';
import { KeyValue } from '../../components/KeyValue';
import { LogPane } from '../../components/LogPane';
import { ProgressBar } from '../../components/ProgressBar';
import { Empty, LoadError, Loading } from '../../components/States';
import { StatusChip } from '../../components/StatusChip';
import { Table } from '../../components/Table';
import { Tag } from '../../components/Tag';
import { toast, toastError } from '../../components/Toast';
import { Tooltip } from '../../components/Tooltip';
import { formatBytes } from '../../lib/format';
import { useApi } from '../../lib/use-api';
import { downloads, engine, settings } from '../../store';
import { t } from '../../strings/welcome';
import { FitTag } from '../models/bits';
import { DownloadsPanel } from '../models/DownloadsPanel';
import { applyPreset, getCatalog, getEffective, getPresets, getSchema, loadEngine, markCompleted, queueDownload } from './api';
import { StepLayout, progress, updateProgress, useWizard } from './frame';
import { closeWelcome, openURL } from './host';
import { curlSample, endpoints, movedOrigin, presetDiff, recommendation } from './logic';
import { probeOrigin } from '../settings/api';
import type { PresetId } from './steps';

const ORDER: PresetId[] = ['coding', 'chat', 'speed'];

// ---------- step 3 ----------

export function StepUseCase() {
  const { goTo } = useWizard();
  const presets = useApi(getPresets);
  const effective = useApi(getEffective);
  const schema = useApi(getSchema);
  const [selected, setSelected] = useState<PresetId>(progress.value.preset ?? 'chat');
  const [busy, setBusy] = useState(false);
  const list = presets.data?.presets ?? [];
  const sorted = ORDER.map((id) => list.find((p) => p.id === id)).filter((p): p is NonNullable<typeof p> => !!p);
  const rows = presets.data ? presetDiff(list, selected, effective.data, schema.data, settings.value?.settings?.global) : [];
  async function apply() {
    setBusy(true);
    try {
      await applyPreset(selected);
      updateProgress({ preset: selected });
      goTo(4);
    } catch (err) {
      toastError(t('welcome.usecase.apply_failed'), err);
    } finally {
      setBusy(false);
    }
  }
  return (
    <StepLayout lead={t('welcome.usecase.lead')} footer={{ onContinue: () => void apply(), busy, continueDisabled: !presets.data }}>
      {presets.error ? (
        <LoadError thing={t('welcome.usecase.thing')} error={presets.error} onRetry={presets.reload} />
      ) : !presets.data ? (
        <Loading />
      ) : (
        <>
          <div class="wz-presets" role="radiogroup" aria-label={t('welcome.usecase.label')}>
            {sorted.map((p) => (
              <label key={p.id} class="wz-preset" data-selected={selected === p.id ? 'true' : undefined}>
                <input type="radio" name="wz-preset" class="visually-hidden" checked={selected === p.id} onChange={() => setSelected(p.id)} />
                <span class="wz-preset-mark" aria-hidden="true">
                  {selected === p.id ? '●' : '○'}
                </span>
                <span class="heading wz-preset-name">{p.label}</span>
                <span class="body">{p.description}</span>
              </label>
            ))}
          </div>
          <h3 class="label">{t('welcome.usecase.applies')}</h3>
          <Table
            caption={t('welcome.usecase.applies')}
            rows={rows}
            rowKey={(r) => r.key}
            columns={[
              { key: 'label', label: t('welcome.usecase.col.setting'), render: (r) => <span class={r.changed ? undefined : 'mute'}>{r.label}</span> },
              { key: 'flag', label: t('welcome.usecase.col.flag'), render: (r) => <span class="mono mute">{r.flag ?? r.key}</span> },
              { key: 'now', label: t('welcome.usecase.col.now'), render: (r) => <span class={r.changed ? undefined : 'mute'}>{r.nowText}{r.nowIsDefault ? ` ${t('welcome.usecase.default')}` : ''}</span> },
              {
                key: 'next',
                label: t('welcome.usecase.col.next'),
                render: (r) =>
                  r.changed ? (
                    r.dependsOnRam ? (
                      <Tooltip text={t('welcome.usecase.ram_tip')}>
                        <span tabIndex={0}>→ {r.nextText}</span>
                      </Tooltip>
                    ) : (
                      <span>→ {r.nextText}</span>
                    )
                  ) : (
                    <span class="mute">
                      → {r.nextText} {t('welcome.usecase.unchanged')}
                    </span>
                  ),
              },
            ]}
          />
        </>
      )}
    </StepLayout>
  );
}

// ---------- step 4 ----------

export function StepModel() {
  const { goTo, hosted } = useWizard();
  const presets = useApi(getPresets);
  const catalog = useApi(getCatalog);
  const installed = useApi((s) => api.get<InstalledModels>('/models', undefined, s));
  const ids = installed.data?.models.map((m) => m.id) ?? [];
  const rec = recommendation(presets.data, progress.value.preset, catalog.data, ids);
  const [busy, setBusy] = useState<string | null>(null);
  async function download(model: string, languageOnly: boolean) {
    setBusy(model);
    try {
      const item = await queueDownload({ id: model, language_only: languageOnly, verify: false });
      updateProgress({ model, downloadId: item.id });
      toast(t('welcome.model.queued', { model }));
    } catch (err) {
      toastError(t('welcome.model.download_failed'), err);
    } finally {
      setBusy(null);
    }
  }
  const browse = '/admin/models/downloader?tab=supported';
  return (
    <StepLayout lead={t('welcome.model.lead')} footer={{ onContinue: () => goTo(5), note: progress.value.downloadId ? t('welcome.model.continue_note') : undefined }}>
      {presets.error ? (
        <LoadError thing={t('welcome.model.thing')} error={presets.error} onRetry={presets.reload} />
      ) : !rec ? (
        <Loading />
      ) : (
        <>
          <p class="body">{t('welcome.model.reason', { preset: rec.preset?.label ?? '', ram: formatBytes(rec.memoryBytes) })}</p>
          {rec.reason && <p class="meta">{rec.reason}</p>}
          <ul class="wz-models">
            {rec.rows.map((r, i) => {
              const chosen = progress.value.model === r.model;
              return (
                <li key={r.model} class="wz-model" data-primary={r.primary ? 'true' : undefined}>
                  {i > 0 && !r.primary && rec.rows[i - 1]?.primary && <h3 class="label wz-alt">{t('welcome.model.alternatives')}</h3>}
                  <div class="wz-model-head">
                    <span class="heading wz-model-name">{r.model.slice(r.model.indexOf('/') + 1)}</span>
                    <span class="cluster">
                      {r.primary && <Tag tone="acc">{t('welcome.model.recommended')}</Tag>}
                      {r.languageOnly && <Tag>{t('welcome.model.language_only')}</Tag>}
                      {r.installed && <Tag>{t('welcome.model.installed')}</Tag>}
                      <span class="label">
                        {r.format?.toUpperCase()}
                        {r.sizeBytes ? ` · ${formatBytes(r.sizeBytes)}` : ''}
                      </span>
                      <FitTag fit={r.fit} needBytes={r.memoryNeedBytes} memoryBytes={rec.memoryBytes} />
                    </span>
                  </div>
                  <p class="meta">
                    <span class="mono">{r.model}</span> · {r.note}
                    {r.perfNote ? ` · ${r.perfNote}` : ''}
                  </p>
                  <div class="cluster">
                    {r.installed ? (
                      <Button size="s" variant={chosen ? 'solid' : r.primary ? 'accent' : 'outline'} onClick={() => updateProgress({ model: r.model, downloadId: null })}>
                        {chosen ? t('welcome.model.selected') : t('welcome.model.use')}
                      </Button>
                    ) : r.disabled ? (
                      <span class="meta">{t('welcome.model.wont_fit', { size: r.memoryNeedBytes ? formatBytes(r.memoryNeedBytes) : '—' })}</span>
                    ) : (
                      <Button size="s" variant={r.primary ? 'accent' : 'outline'} loading={busy === r.model} disabled={chosen && !!progress.value.downloadId} onClick={() => void download(r.model, r.languageOnly)}>
                        {r.sizeBytes ? t('welcome.model.download_size', { size: formatBytes(r.sizeBytes) }) : t('welcome.model.download')}
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
          {hosted ? (
            <Button variant="text" onClick={() => void openURL(new URL(browse, location.origin).href)}>
              {t('welcome.model.browse')} ↗
            </Button>
          ) : (
            <a href={browse} target="_blank" rel="noopener noreferrer">
              {t('welcome.model.browse')} ↗
            </a>
          )}
          {progress.value.downloadId && <DownloadsPanel installedIds={new Set(ids)} />}
        </>
      )}
    </StepLayout>
  );
}

// ---------- step 5 ----------

const PHASES = ['installing', 'loading', 'warming', 'ready'] as const;

export function StepStart() {
  const { goTo, hosted } = useWizard();
  const [, navigate] = useLocation();
  const model = progress.value.model;
  const e = engine.value;
  const ready = !!e && e.model === model && ['ready', 'busy', 'idle_released'].includes(e.state);
  const installed = useApi((s) => api.get<InstalledModels>('/models', undefined, s), [downloads.value.length]);
  const dl = downloads.value.find((d) => d.id === progress.value.downloadId || d.model === model);
  const isInstalled = !!model && (installed.data?.models.some((m) => m.id === model) ?? false);
  const [busy, setBusy] = useState(false);
  const [completed, setCompleted] = useState(false);
  const keyRequired = !!(settings.value?.settings?.global?.security as { api_key_required?: boolean } | undefined)?.api_key_required;
  const shim = useApi((s) => api.get<{ on_path: boolean }>('/cli/shim', undefined, s), [], ready);
  const [pathBusy, setPathBusy] = useState(false);

  const [moving, setMoving] = useState<string | null>(null);
  useEffect(() => {
    if (!ready || completed) return;
    setCompleted(true);
    const port = progress.value.pendingPort;
    void markCompleted((doc) => {
      doc.global.routing = { ...((doc.global.routing ?? {}) as object), default_model: model } as never;
      if (port) doc.global.server = { ...((doc.global.server ?? { host: '127.0.0.1', port: 8000 }) as object), port } as never;
    })
      .then(async () => {
        // W2: the port typed in step 2 applies now; follow the manager to its new address.
        if (!port || String(port) === location.port) return;
        const origin = movedOrigin(location.origin, port);
        setMoving(origin);
        const until = Date.now() + 20_000;
        while (Date.now() < until) {
          if (await probeOrigin(origin, 500)) {
            location.replace(`${origin}${location.pathname}${location.search}`);
            return;
          }
          await new Promise((r) => setTimeout(r, 500));
        }
        setMoving(null);
      })
      .catch((err) => toastError(t('welcome.finish_failed'), err));
  }, [ready]);

  async function load() {
    if (!model) return;
    setBusy(true);
    try {
      await loadEngine(model);
    } catch (err) {
      toastError(t('welcome.start.load_failed'), err);
    } finally {
      setBusy(false);
    }
  }
  async function finish(path: string) {
    if (!completed) {
      try {
        await markCompleted();
      } catch (err) {
        toastError(t('welcome.finish_failed'), err);
        return;
      }
    }
    if (hosted) {
      await openURL(new URL(`/admin${path}`, location.origin).href).catch(() => undefined);
      await closeWelcome(true).catch(() => undefined);
      toast(t('welcome.start.close_hint'));
    } else navigate(path);
  }

  if (!model)
    return (
      <StepLayout lead={t('welcome.start.lead')} footer={null}>
        <Empty
          title={t('welcome.start.no_model')}
          action={
            <>
              <Button onClick={() => goTo(4)}>{t('welcome.start.back_model')}</Button>
              <Button variant="text" onClick={() => void finish('/status')}>
                {t('welcome.start.finish_anyway')}
              </Button>
            </>
          }
        >
          {t('welcome.start.no_model_body')}
        </Empty>
      </StepLayout>
    );

  if (ready) {
    const ep = endpoints(location.origin);
    return (
      <StepLayout lead={t('welcome.start.ready_title')} footer={null}>
        <p class="cluster">
          <span class="mono">{model}</span> <StatusChip state={e?.state} announce={false} />
        </p>
        {moving && (
          <p class="meta loading-dots" role="status">
            {t('welcome.start.moving', { url: moving })}
          </p>
        )}
        <KeyValue
          label={t('welcome.start.endpoints')}
          items={[
            { key: 'oa', label: t('welcome.start.openai'), value: <span class="cluster"><code class="mono">{ep.openai}</code><CopyButton text={ep.openai} /></span> },
            { key: 'an', label: t('welcome.start.anthropic'), value: <span class="cluster"><code class="mono">{ep.anthropic}</code><CopyButton text={ep.anthropic} /></span> },
            { key: 'key', label: t('welcome.start.key'), value: keyRequired ? t('welcome.start.key_on') : t('welcome.start.key_off') },
          ]}
        />
        <h3 class="label">{t('welcome.start.try')}</h3>
        <CodeBlock code={curlSample(location.origin, model, keyRequired)} />
        <h3 class="label">{t('welcome.start.next')}</h3>
        <div class="cluster">
          <Button variant="accent" onClick={() => void finish('/chat')}>
            {t('welcome.start.open_chat')}
          </Button>
          <Button onClick={() => void finish('/integrations')}>{t('welcome.start.agents')} ↗</Button>
          <Button variant="text" onClick={() => void finish('/status')}>
            {t('welcome.start.open_status')}
          </Button>
        </div>
        {shim.data?.on_path ? (
          <p class="meta">{t('welcome.start.path_on')}</p>
        ) : (
          <div class="stack">
            <p class="body">{t('welcome.start.path')}</p>
            <div class="cluster">
              <Button
                size="s"
                loading={pathBusy}
                onClick={() => {
                  setPathBusy(true);
                  void api
                    .post('/cli/shim', { add_to_path: true })
                    .then(() => {
                      toast(t('welcome.start.path_done'));
                      void shim.reload();
                    })
                    .catch((err) => toastError(t('welcome.start.path_failed'), err))
                    .finally(() => setPathBusy(false));
                }}
              >
                {t('welcome.start.path_add')}
              </Button>
              <span class="meta">{t('welcome.start.path_help')}</span>
            </div>
          </div>
        )}
      </StepLayout>
    );
  }

  const failed = e?.state === 'failed' && e.model === model;
  const phase = e?.model === model && e.state.startsWith('starting') ? (e.phase ?? 'loading') : null;
  const tail = ((e?.view?.log_tail as string[] | undefined) ?? []).slice(-12);
  return (
    <StepLayout lead={t('welcome.start.lead')} footer={{ hideContinue: true }}>
      {dl && !isInstalled && (
        <div class="stack">
          <span class="label">
            {t('welcome.start.download')} · <span class="mono">{model}</span> · {dl.state}
          </span>
          <ProgressBar live value={dl.bytes_total ? (dl.bytes_done ?? 0) / dl.bytes_total : (dl.progress ?? null)} label={t('welcome.start.download')} />
        </div>
      )}
      <div class="cluster">
        <Button variant="accent" disabled={!isInstalled || busy || !!phase} loading={busy || !!phase} onClick={() => void load()}>
          {t('welcome.start.load')}
        </Button>
        <span class="meta">{isInstalled ? t('welcome.start.load_help') : t('welcome.start.waiting_download')}</span>
      </div>
      {phase && (
        <ol class="wz-phases cluster" aria-live="polite">
          {PHASES.map((p) => {
            const idx = PHASES.indexOf(p);
            const cur = PHASES.indexOf(phase as (typeof PHASES)[number]);
            return (
              <li key={p} class={idx < cur ? undefined : idx === cur ? 'loading-dots' : 'mute'}>
                {t(`welcome.start.phase.${p}`)}
                {idx < cur ? ' ✓' : ''}
              </li>
            );
          })}
        </ol>
      )}
      {(phase || failed) && tail.length > 0 && <LogPane label={t('welcome.start.log')} lines={tail.map((text, i) => ({ key: i, text }))} height={200} />}
      {failed && (
        <Banner
          tone="critical"
          title={t('welcome.start.failed')}
          actions={
            <span class="cluster">
              <Button size="s" onClick={() => void load()}>
                {t('welcome.start.retry')}
              </Button>
              <Button size="s" variant="text" onClick={() => goTo(4)}>
                {t('welcome.start.choose_other')}
              </Button>
            </span>
          }
        >
          <code class="mono">{e?.error?.message ?? ''}</code>
        </Banner>
      )}
    </StepLayout>
  );
}
