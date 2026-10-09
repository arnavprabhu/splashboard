/**
 * Step 2 — Storage & server: models and cache folders, import from the
 * Hugging Face cache, port (applied at step 5, W2), optional API key, launch at login.
 */

import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { IssueOut, JobEvent, SettingsResponse } from '../../api/models';
import { ApiError } from '../../api/client';
import {
  Banner,
  Button,
  Checkbox,
  CopyButton,
  LoadError,
  Loading,
  NumberInput,
  ProgressBar,
  Sheet,
  TextInput,
  Toggle,
  toast,
  toastError,
} from '../../components';
import { formatBytes } from '../../lib/format';
import { useApi } from '../../lib/use-api';
import { useEvent } from '../../store';
import { t } from '../../strings/welcome';
import { generateApiKey, getImportCandidates, getSettings, getStorage, importModels, saveSettings, validateSettings, withEdit } from './api';
import { progress, StepLayout, updateProgress, useWizard } from './frame';
import { HostError, pickFolder } from './host';
import { maskKey } from './logic';
import { nextStep } from './steps';

export const PORT_MIN = 1024;
export const PORT_MAX = 65535;
const LEGACY_PORT = 9999;

type Target = 'models' | 'cache';

/** Client-side path check before asking the manager: a full path (`/…` or `~/…`). */
export function pathError(path: string): string | null {
  const p = path.trim();
  if (!p) return t('welcome.storage.path_invalid');
  return p.startsWith('/') || p === '~' || p.startsWith('~/') ? null : t('welcome.storage.path_invalid');
}

export function portRangeError(port: number | null): string | null {
  if (port === null || !Number.isInteger(port) || port < PORT_MIN || port > PORT_MAX) return t('welcome.storage.port_range');
  return null;
}

/** The validation issue about `server.port`, if any (`port_in_use`, `port_conflict`). */
export function portIssue(issues: readonly IssueOut[]): IssueOut | null {
  return issues.find((i) => i.key === 'server.port' || i.path.join('.').endsWith('server.port')) ?? null;
}

type PortCheck = { kind: 'idle' } | { kind: 'checking' } | { kind: 'ok' } | { kind: 'error'; message: string };

function FolderSheet({ target, initial, onClose, onPick }: { target: Target | null; initial: string; onClose: () => void; onPick: (path: string | null) => void }) {
  const [value, setValue] = useState(initial);
  useEffect(() => setValue(initial), [initial, target]);
  const error = value ? pathError(value) : null;
  return (
    <Sheet
      open={target !== null}
      title={target === 'cache' ? t('welcome.storage.sheet.cache') : t('welcome.storage.sheet.models')}
      onClose={onClose}
      testId="folder-sheet"
      footer={
        <div class="cluster">
          <Button variant="solid" disabled={!value || !!error} onClick={() => onPick(value.trim())}>
            {t('welcome.storage.sheet.use')}
          </Button>
          <Button onClick={() => onPick(null)}>{t('welcome.storage.sheet.default')}</Button>
        </div>
      }
    >
      <div class="stack">
        <label class="label" for="wz-folder">
          {t('welcome.storage.sheet.path')}
        </label>
        <TextInput id="wz-folder" class="mono" value={value} onChange={setValue} invalid={!!error} spellcheck={false} autocomplete="off" aria-describedby="wz-folder-help" />
        <p id="wz-folder-help" class={error ? 'field-error' : 'body mute'}>
          {error ?? t('welcome.storage.sheet.help')}
        </p>
      </div>
    </Sheet>
  );
}

export function StepStorage() {
  const { step, goTo, hosted, system } = useWizard();
  const settingsReq = useApi((s) => getSettings(s), []);
  const storage = useApi((s) => getStorage(s), []);
  const s: SettingsResponse | null = settingsReq.data;
  const g = s?.settings.global;

  // Draft (written on Continue; the port is kept for step 5).
  const [models, setModels] = useState<string | null | undefined>(undefined); // undefined = unchanged, null = default
  const [cache, setCache] = useState<string | null | undefined>(undefined);
  const [port, setPort] = useState<number | null>(null);
  const [keyOn, setKeyOn] = useState<boolean | null>(null);
  const [launch, setLaunch] = useState<boolean | null>(null);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [keyBusy, setKeyBusy] = useState(false);
  const [sheet, setSheet] = useState<Target | null>(null);
  const [check, setCheck] = useState<PortCheck>({ kind: 'idle' });
  const [saving, setSaving] = useState(false);
  const [saveIssues, setSaveIssues] = useState<IssueOut[] | null>(null);

  // Acceptance 1.3 F2: "current" is where the manager listens, which differs from
  // `server.port` when it was started with `--port`; the stored port may belong to
  // another process (oMLX on 8000), so it is never proposed unchecked.
  const storedPort = g?.server?.port ?? 8000;
  const currentPort = s?.listening_port ?? storedPort;
  useEffect(() => {
    if (!g) return;
    setPort((p) => p ?? progress.value.pendingPort ?? currentPort);
    setKeyOn((k) => k ?? Boolean(g.security?.api_key_required));
    setLaunch((l) => l ?? g.lifecycle?.launch_at_login ?? true);
  }, [g, currentPort]);

  // Live port check through POST /settings/validate (debounced).
  const seq = useRef(0);
  useEffect(() => {
    if (!s || port === null) return;
    const range = portRangeError(port);
    if (range) {
      setCheck({ kind: 'error', message: range });
      return;
    }
    if (port === currentPort) {
      setCheck({ kind: 'ok' });
      return;
    }
    setCheck({ kind: 'checking' });
    const mine = ++seq.current;
    const timer = setTimeout(() => {
      validateSettings(
        withEdit(s.settings, (d) => {
          d.global.server = { ...(d.global.server ?? { host: '127.0.0.1', port }), port };
        }),
      )
        .then((v) => {
          if (mine !== seq.current) return;
          // A busy stored port comes back as a warning (saving it is allowed); here it is a block.
          const issue = portIssue([...v.errors, ...v.warnings]);
          if (!issue) setCheck({ kind: 'ok' });
          else setCheck({ kind: 'error', message: issue.code === 'port_in_use' ? t('welcome.storage.port_in_use', { port: String(port) }) : issue.message });
        })
        .catch(() => mine === seq.current && setCheck({ kind: 'idle' }));
    }, 350);
    return () => clearTimeout(timer);
  }, [s, port, currentPort]);

  const resolvedModels = s?.resolved.models_dir ?? '';
  const resolvedCache = s?.resolved.cache_dir ?? '';
  const shownModels = models === undefined ? resolvedModels : (models ?? t('welcome.storage.default_models'));
  const shownCache = cache === undefined ? resolvedCache : (cache ?? t('welcome.storage.default_cache'));

  const change = async (target: Target) => {
    const current = target === 'models' ? (models ?? resolvedModels) : (cache ?? resolvedCache);
    if (hosted) {
      try {
        const picked = await pickFolder(target, current);
        if (picked) (target === 'models' ? setModels : setCache)(picked);
      } catch (err) {
        if (err instanceof HostError) toastError(t('welcome.storage.pick_failed'), err);
      }
      return;
    }
    setSheet(target);
  };

  const toggleKey = async (on: boolean) => {
    setKeyOn(on);
    if (on && !s?.secrets.api_key_set && !newKey) await makeKey();
  };

  const makeKey = async () => {
    setKeyBusy(true);
    try {
      const out = await generateApiKey();
      setNewKey(out.key);
      void settingsReq.reload();
    } catch (err) {
      toastError(t('welcome.storage.key_failed'), err);
      setKeyOn(false);
    } finally {
      setKeyBusy(false);
    }
  };

  const onContinue = async () => {
    setSaving(true);
    setSaveIssues(null);
    try {
      await saveSettings((d) => {
        d.global.storage = { ...(d.global.storage ?? {}) };
        if (models !== undefined) d.global.storage.models_dir = models;
        if (cache !== undefined) d.global.storage.cache_dir = cache;
        d.global.security = { ...(d.global.security ?? { admin_requires_key: false, api_key_required: false }), api_key_required: Boolean(keyOn) };
        d.global.lifecycle = { ...(d.global.lifecycle ?? { stop_on_quit: true, auto_restart: true, idle_unload: false, idle_unload_minutes: 30, launch_at_login: true }), launch_at_login: Boolean(launch) };
      });
      // Step 5 saves the port when it differs from the stored one, even if it is where the
      // manager already listens, so the next start (menu bar, `splash start`) uses it too.
      updateProgress({ pendingPort: port !== null && port !== storedPort ? port : null });
      goTo(nextStep(step)!);
    } catch (err) {
      if (err instanceof ApiError && err.issues.length > 0) setSaveIssues(err.issues as unknown as IssueOut[]);
      else toastError(t('welcome.save_failed'), err);
    } finally {
      setSaving(false);
    }
  };

  const portChanged = port !== null && port !== currentPort;
  const blocked = check.kind === 'error' || check.kind === 'checking' || (keyOn === true && keyBusy);

  if (settingsReq.error) {
    return (
      <StepLayout lead={t('welcome.storage.lead')} footer={{ continueDisabled: true, onContinue: () => undefined }}>
        <LoadError thing={t('welcome.storage.settings')} error={settingsReq.error} onRetry={() => void settingsReq.reload()} />
      </StepLayout>
    );
  }
  if (!s) {
    return (
      <StepLayout lead={t('welcome.storage.lead')} footer={{ continueDisabled: true, onContinue: () => undefined }}>
        <Loading />
      </StepLayout>
    );
  }

  const modelsFree = system?.disk?.models?.free_bytes;
  const sameVolume = system && system.disk?.models?.path === system.disk?.cache?.path;
  const modelsBytes = storage.data?.models_bytes;
  const cacheBytes = storage.data?.cache_bytes;

  return (
    <StepLayout
      lead={t('welcome.storage.lead')}
      footer={{
        onContinue: () => void onContinue(),
        continueDisabled: blocked,
        busy: saving,
        note: portChanged ? t('welcome.storage.port_applies') : undefined,
      }}
    >
      <dl class="kv wz-fields">
        <div class="kv-row wz-field" data-testid="field-models">
          <dt class="label">{t('welcome.storage.row.models')}</dt>
          <dd class="wz-field-dd">
            <span class="mono wz-path">{shownModels}</span>
            <span class="meta tnum">
              {models === undefined && modelsBytes ? t('welcome.storage.contains', { size: formatBytes(modelsBytes) }) + ' · ' : ''}
              {models === undefined && modelsFree != null ? t('welcome.storage.free', { size: formatBytes(modelsFree, { digits: 0 }) }) : ''}
            </span>
            <Button size="s" onClick={() => void change('models')} aria-label={t('welcome.storage.change_models')}>
              {t('welcome.storage.change')}
            </Button>
          </dd>
        </div>
        <div class="kv-row wz-field" data-testid="field-cache">
          <dt class="label">{t('welcome.storage.row.cache')}</dt>
          <dd class="wz-field-dd">
            <span class="mono wz-path">{shownCache}</span>
            <span class="meta tnum">
              {cache === undefined && cacheBytes ? t('welcome.storage.contains', { size: formatBytes(cacheBytes) }) + ' · ' : ''}
              {cache === undefined && system ? (sameVolume ? t('welcome.storage.same_volume') : t('welcome.storage.free', { size: formatBytes(system.disk?.cache?.free_bytes ?? null, { digits: 0 }) })) : ''}
            </span>
            <Button size="s" onClick={() => void change('cache')} aria-label={t('welcome.storage.change_cache')}>
              {t('welcome.storage.change')}
            </Button>
          </dd>
        </div>
        <div class="kv-row wz-field" data-testid="field-port">
          <dt class="label">
            <label for="wz-port">{t('welcome.storage.row.port')}</label>
          </dt>
          <dd class="wz-field-dd">
            <NumberInput
              id="wz-port"
              class="wz-port tnum"
              value={port}
              onChange={setPort}
              min={PORT_MIN}
              max={PORT_MAX}
              step={1}
              invalid={check.kind === 'error'}
              aria-describedby="wz-port-status"
            />
            <span id="wz-port-status" class={check.kind === 'error' ? 'field-error' : 'meta'} role={check.kind === 'error' ? 'alert' : undefined} data-testid="port-status">
              {check.kind === 'error'
                ? check.message
                : check.kind === 'checking'
                  ? <span class="loading-dots">{t('welcome.storage.port_checking')}</span>
                  : port === currentPort
                    ? t('welcome.storage.port_current')
                    : `${t('welcome.storage.port_free')} ✓`}
            </span>
            <span class="meta mono wz-url">{`http://127.0.0.1:${port ?? currentPort}`}</span>
            {port === LEGACY_PORT && <p class="meta wz-full">{t('welcome.storage.port_legacy')}</p>}
          </dd>
        </div>
        <div class="kv-row wz-field" data-testid="field-key">
          <dt class="label">{t('welcome.storage.row.key')}</dt>
          <dd class="wz-field-dd">
            <Toggle checked={Boolean(keyOn)} onChange={(on) => void toggleKey(on)} label={t('welcome.storage.row.key')} disabled={keyBusy} />
            {!keyOn && <span class="body mute wz-grow">{t('welcome.storage.key_off')}</span>}
            {keyOn && (
              <div class="stack wz-tight wz-grow">
                {keyBusy && <Loading label={t('welcome.storage.key_making')} />}
                {newKey ? (
                  <>
                    <div class="cluster">
                      <code class="mono wz-key" data-testid="api-key">{newKey}</code>
                      <CopyButton text={newKey} what={t('welcome.storage.row.key')} />
                      <Button size="s" variant="text" onClick={() => void makeKey()} disabled={keyBusy}>
                        {t('welcome.storage.key_regenerate')}
                      </Button>
                    </div>
                    <p class="meta">{t('welcome.storage.key_once')}</p>
                  </>
                ) : (
                  s.secrets.api_key_set && (
                    <div class="cluster">
                      <code class="mono">{maskKey(null) === '—' ? t('welcome.storage.key_set') : ''}</code>
                      <Button size="s" variant="text" onClick={() => void makeKey()} disabled={keyBusy}>
                        {t('welcome.storage.key_regenerate')}
                      </Button>
                    </div>
                  )
                )}
                <p class="meta">
                  {t('welcome.storage.key_stored')} {t('welcome.storage.key_clients')}
                </p>
              </div>
            )}
          </dd>
        </div>
        <div class="kv-row wz-field" data-testid="field-login">
          <dt class="label">{t('welcome.storage.row.login')}</dt>
          <dd class="wz-field-dd">
            <Toggle checked={Boolean(launch)} onChange={setLaunch} label={t('welcome.storage.row.login')} />
            <span class="body mute wz-grow">{t('welcome.storage.login_help')}</span>
          </dd>
        </div>
      </dl>

      {saveIssues && (
        <Banner tone="critical" title={t('welcome.save_failed')}>
          <ul class="wz-issues">
            {saveIssues.map((i) => (
              <li key={i.path.join('.')}>
                <code class="mono">{i.key}</code> {i.message}
              </li>
            ))}
          </ul>
        </Banner>
      )}

      <ImportBlock modelsDir={models ?? resolvedModels} />

      <FolderSheet
        target={sheet}
        initial={sheet === 'cache' ? (cache ?? resolvedCache) : (models ?? resolvedModels)}
        onClose={() => setSheet(null)}
        onPick={(path) => {
          if (sheet === 'models') setModels(path);
          else if (sheet === 'cache') setCache(path);
          setSheet(null);
        }}
      />
    </StepLayout>
  );
}

// ---------- import ----------

function ImportBlock({ modelsDir }: { modelsDir: string }) {
  const req = useApi((s) => getImportCandidates(s), []);
  const [unchecked, setUnchecked] = useState<ReadonlySet<string>>(new Set());
  const [job, setJob] = useState<{ id: string | null; progress: number | null; message: string | null; state: 'running' | 'done' | 'failed' } | null>(null);
  const candidates = useMemo(() => req.data?.candidates ?? [], [req.data]);
  const selected = candidates.filter((c) => !unchecked.has(c.repo_id));
  const total = candidates.reduce((sum, c) => sum + c.size_bytes, 0);

  useEvent('job', (data) => {
    const ev = data as Partial<JobEvent> | null;
    if (!ev || ev.kind !== 'import' || !job || job.state !== 'running') return;
    if (job.id && ev.job_id && ev.job_id !== job.id) return;
    const next = { id: job.id, progress: ev.progress ?? job.progress, message: ev.message ?? job.message, state: (ev.state ?? 'running') as 'running' | 'done' | 'failed' };
    setJob(next);
    if (ev.state === 'done') {
      toast(t('welcome.import.done', { n: selected.length, dir: modelsDir }));
      void req.reload();
    }
  });

  if (req.loading && !req.data) return null;
  if (req.error) return <LoadError thing={t('welcome.import.thing')} error={req.error} onRetry={() => void req.reload()} />;
  if (req.data === null) {
    return (
      <div class="wz-sub stack wz-tight" data-testid="import-unavailable">
        <h3 class="label">{t('welcome.import.label')}</h3>
        <p class="meta">{t('welcome.import.unavailable')}</p>
      </div>
    );
  }
  if (candidates.length === 0) return null;

  const move = async () => {
    setJob({ id: null, progress: null, message: null, state: 'running' });
    try {
      const accepted = await importModels(selected.map((c) => c.repo_id));
      setJob((j) => (j ? { ...j, id: accepted.job_id } : j));
    } catch (err) {
      setJob({ id: null, progress: null, message: err instanceof Error ? err.message : String(err), state: 'failed' });
    }
  };

  return (
    <div class="wz-sub stack" data-testid="import">
      <h3 class="label">{t('welcome.import.label')}</h3>
      <p class="body">
        {t('welcome.import.found', { n: candidates.length, dir: req.data.source_dir, size: formatBytes(total) })}
      </p>
      <ul class="wz-import-list">
        {candidates.map((c) => (
          <li key={c.repo_id} class="wz-import-row">
            <Checkbox
              checked={!unchecked.has(c.repo_id)}
              disabled={job?.state === 'running'}
              onChange={(on) =>
                setUnchecked((prev) => {
                  const next = new Set(prev);
                  if (on) next.delete(c.repo_id);
                  else next.add(c.repo_id);
                  return next;
                })
              }
              label={
                <span class="mono">
                  {c.repo_id}
                  {c.kind === 'draft' ? ` (${t('welcome.import.draft')})` : ''}
                </span>
              }
            />
            <span class="meta tnum">{formatBytes(c.size_bytes)}</span>
          </li>
        ))}
      </ul>
      <div class="cluster">
        <Button onClick={() => void move()} disabled={selected.length === 0} loading={job?.state === 'running'} data-testid="import-move">
          {t('welcome.import.move', { n: selected.length, dir: modelsDir })}
        </Button>
        <p class="meta">{t('welcome.import.move_help')}</p>
      </div>
      {job?.state === 'running' && (
        <div class="stack wz-tight">
          <ProgressBar value={job.progress} label={t('welcome.import.label')} />
          {job.message && <p class="meta loading-dots">{t('welcome.import.moving', { name: job.message })}</p>}
        </div>
      )}
      {job?.state === 'failed' && <Banner tone="warn">{t('welcome.import.failed', { error: job.message ?? '' })}</Banner>}
    </div>
  );
}
