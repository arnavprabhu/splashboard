import { useEffect, useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import type { DownloadItem } from '../../api/models';
import { Button } from '../../components/Button';
import { Disclosure } from '../../components/Disclosure';
import { Select } from '../../components/inputs';
import { LogPane } from '../../components/LogPane';
import { ProgressBar } from '../../components/ProgressBar';
import { Tag } from '../../components/Tag';
import { toast, toastError } from '../../components/Toast';
import { formatBytes, formatBytesPerSecond, formatDuration, formatRelativeTime } from '../../lib/format';
import { isRunning } from '../../lib/engine-state';
import { downloads, engineState, loadSettings } from '../../store';
import { t } from '../../strings/models';
import * as flows from './actions';
import { verifyModel } from './api';
import { CancelSheet } from './CancelSheet';
import { ACTIVE_DOWNLOAD_STATES, downloadErrorView, itemProgress, percent, shortName, summarizeDownloads, type DownloadAction } from './logic';
import { globalSetting, writeGlobalSetting } from './settings';

const HUB = { base: 1000 } as const;

/** Done rows hidden with Dismiss in this tab (no API removes a finished record). */
const dismissed = new Set<string>();

export interface DownloadsPanelProps {
  installedIds: ReadonlySet<string>;
}

/**
 * The downloads queue (03 §3.5, SPEC §9.4): sticky at the bottom of the Downloader while
 * anything is active, overall bar, per-file expansion, speed, ETA, pause/resume/cancel,
 * every error with its action, and the installer log from `log_tail`.
 */
export function DownloadsPanel({ installedIds }: DownloadsPanelProps) {
  const [, rerender] = useState(0);
  const [cancelling, setCancelling] = useState<DownloadItem | null>(null);
  const [history, setHistory] = useState(false);
  const all = downloads.value.filter((d) => !dismissed.has(d.id));
  const summary = summarizeDownloads(all);
  const active = all.filter((d) => ACTIVE_DOWNLOAD_STATES.has(d.state));
  const finished = all.filter((d) => !ACTIVE_DOWNLOAD_STATES.has(d.state));
  const failed = finished.filter((d) => d.state === 'failed');
  const sticky = summary.active > 0;
  const parallel = globalSetting<number>('downloads', 'parallel', 1);

  useEffect(() => {
    void loadSettings();
    if (location.hash === '#downloads') requestAnimationFrame(() => document.getElementById('downloads')?.scrollIntoView({ block: 'nearest' }));
  }, []);

  const shown = sticky || history ? [...active, ...finished.slice().reverse()] : failed;
  const lastDone = finished.filter((d) => d.state === 'done').at(-1);

  const setParallel = async (v: string) => {
    try {
      await writeGlobalSetting('downloads', 'parallel', Number(v));
      toast(t('models.dl.parallel_saved', { n: Number(v) }));
    } catch (err) {
      toastError(t('models.dl.parallel_failed'), err);
    }
  };

  return (
    <section id="downloads" class="band dl-panel" data-sticky={sticky ? 'true' : 'false'} aria-labelledby="downloads-label" data-testid="downloads">
      <div class="dl-head">
        <h2 class="label" id="downloads-label">
          {t('models.dl.label')}
        </h2>
        <p class="meta tnum dl-summary" data-testid="downloads-summary">
          {summary.active === 0
            ? lastDone
              ? t('models.dl.last', { model: shortName(lastDone.model), when: formatRelativeTime(lastDone.finished_at ?? lastDone.created_at) })
              : all.length === 0
                ? t('models.dl.none')
                : t('models.dl.idle')
            : [
                summary.running + summary.verifying > 0 ? t('models.dl.n_active', { n: summary.running + summary.verifying }) : null,
                summary.queued > 0 ? t('models.dl.n_queued', { n: summary.queued }) : null,
                summary.paused > 0 ? t('models.dl.n_paused', { n: summary.paused }) : null,
                summary.leftBytes !== null ? t('models.dl.left', { size: formatBytes(summary.leftBytes, HUB) }) : null,
                summary.speedBps ? formatBytesPerSecond(summary.speedBps, HUB) : null,
                summary.etaS !== null && summary.running > 0 ? formatDuration(summary.etaS) : null,
              ]
                .filter(Boolean)
                .join(' · ')}
        </p>
        <span class="cluster dl-head-actions">
          <label class="meta" for="dl-parallel">
            {t('models.dl.parallel')}
          </label>
          <Select
            id="dl-parallel"
            value={String(parallel)}
            options={[1, 2, 3].map((n) => ({ value: String(n), label: String(n) }))}
            onChange={(v) => void setParallel(v)}
          />
          {!sticky && finished.length > 0 && (
            <Button variant="text" size="s" aria-expanded={history} onClick={() => setHistory(!history)}>
              {history ? t('models.dl.history_hide') : t('models.dl.history')}
            </Button>
          )}
        </span>
      </div>
      {sticky && (
        <ProgressBar
          value={summary.progress}
          label={t('models.dl.overall')}
          live={summary.running > 0}
          valueText={t('models.dl.overall_text', { pct: percent(summary.progress) })}
        />
      )}
      {sticky && <p class="meta tnum">{t('models.dl.overall_text', { pct: percent(summary.progress) })}</p>}
      {shown.length > 0 && (
        <ul class="dl-list" aria-label={t('models.dl.list')}>
          {shown.map((d) => (
            <DownloadRow
              key={d.id}
              item={d}
              installed={installedIds.has(d.model)}
              onCancel={() => setCancelling(d)}
              onDismiss={() => {
                dismissed.add(d.id);
                rerender((n) => n + 1);
              }}
            />
          ))}
        </ul>
      )}
      <CancelSheet item={cancelling} onClose={() => setCancelling(null)} />
    </section>
  );
}

function stateLabel(d: DownloadItem): string {
  switch (d.state) {
    case 'queued':
      return t('models.dl.state.queued');
    case 'running':
      return t('models.dl.state.running', { pct: percent(itemProgress(d)) });
    case 'paused':
      return t('models.dl.state.paused');
    case 'verifying':
      return t('models.dl.state.verifying');
    case 'done':
      return t('models.dl.state.done');
    case 'failed':
      return t('models.dl.state.failed');
    default:
      return t('models.dl.state.cancelled');
  }
}

export function downloadMeta(d: DownloadItem): string {
  const parts: string[] = [];
  if (typeof d.bytes_total === 'number') parts.push(t('models.dl.of', { done: formatBytes(d.bytes_done ?? 0, HUB), total: formatBytes(d.bytes_total, HUB) }));
  else if (d.bytes_done) parts.push(formatBytes(d.bytes_done, HUB));
  if (d.state === 'running') {
    if (typeof d.speed_bps === 'number') parts.push(formatBytesPerSecond(d.speed_bps, HUB));
    if (typeof d.eta_s === 'number') parts.push(t('models.dl.eta', { time: formatDuration(d.eta_s) }));
  }
  if (d.state === 'paused') return t('models.dl.paused_kept', { size: formatBytes(d.bytes_done ?? 0, HUB) });
  if (d.state === 'verifying') return d.verify && globalSetting('downloads', 'full_verify', false) ? t('models.dl.full_verify') : t('models.dl.quick_verify');
  return parts.join(' · ');
}

function DownloadRow({ item: d, installed, onCancel, onDismiss }: { item: DownloadItem; installed: boolean; onCancel: () => void; onDismiss: () => void }) {
  const [logOpen, setLogOpen] = useState(false);
  const err = d.state === 'failed' || d.error ? downloadErrorView(d, installed) : null;
  const files = d.files ?? [];
  const log = d.log_tail ?? [];
  const short = shortName(d.model);
  const engineStopped = !isRunning(engineState.value);

  const act = (a: DownloadAction) => {
    switch (a) {
      case 'add_token':
        return (
          <Link key={a} href="/settings/hf#hf.token_override" class="btn" data-size="s" data-testid="dl-add-token">
            {t('models.dl.action.add_token')}
          </Link>
        );
      case 'storage':
        return (
          <Link key={a} href="/settings/storage" class="btn" data-size="s">
            {t('models.dl.action.storage')}
          </Link>
        );
      case 'retry':
      case 'redownload':
        return (
          <Button key={a} size="s" onClick={() => void flows.retry(d)}>
            {a === 'retry' ? t('models.dl.action.retry') : t('models.dl.action.redownload')}
          </Button>
        );
      case 'work_offline':
        return (
          <Button
            key={a}
            size="s"
            onClick={() =>
              void writeGlobalSetting('hf', 'offline', true).then(
                () => toast(t('models.dl.offline_on')),
                (e: unknown) => toastError(t('models.dl.offline_failed'), e),
              )
            }
          >
            {t('models.dl.action.work_offline')}
          </Button>
        );
      case 'details':
        return (
          <Button key={a} size="s" variant="text" onClick={() => setLogOpen(true)}>
            {t('models.dl.action.details')}
          </Button>
        );
      case 'full_verify':
        return (
          <Button
            key={a}
            size="s"
            onClick={() =>
              void verifyModel(d.model, true).then(
                () => toast(t('models.toast.verifying', { model: short })),
                (e: unknown) => toastError(t('models.toast.verify_failed', { short }), e),
              )
            }
          >
            {t('models.dl.action.full_verify')}
          </Button>
        );
      case 'resume':
        return (
          <Button key={a} size="s" onClick={() => void flows.resume(d)}>
            {t('models.dl.action.resume')}
          </Button>
        );
      case 'remove':
      default:
        return (
          <Button key={a} size="s" variant="text" onClick={() => void flows.remove(d, false)}>
            {t('models.dl.action.remove')}
          </Button>
        );
    }
  };

  return (
    <li class="dl-row" data-state={d.state} data-testid="download-row" data-model={d.model}>
      <div class="dl-row-main">
        <span class="mono dl-model">{d.model}</span>
        <Tag tone={d.state === 'failed' || d.state === 'cancelled' ? 'mute' : 'ink'} dots={d.state === 'verifying'}>
          <span data-testid="download-state">{stateLabel(d)}</span>
        </Tag>
        <span class="meta tnum dl-meta">{downloadMeta(d)}</span>
      </div>
      {(d.state === 'running' || d.state === 'paused' || d.state === 'queued') && (
        <ProgressBar
          value={d.state === 'queued' ? 0 : itemProgress(d)}
          label={t('models.dl.item_progress', { model: d.model })}
          live={d.state === 'running'}
          valueText={`${percent(itemProgress(d))} · ${downloadMeta(d)}`}
        />
      )}
      {d.state === 'verifying' && <ProgressBar value={null} label={t('models.dl.item_progress', { model: d.model })} />}
      {err && (
        <p class="body dl-error" role="status" data-testid="download-error">
          {err.text}
        </p>
      )}
      <div class="cluster dl-actions">
        {d.state === 'running' && (
          <Button size="s" onClick={() => void flows.pause(d)} aria-label={t('models.dl.pause_label', { model: d.model })}>
            {t('models.dl.action.pause')}
          </Button>
        )}
        {d.state === 'paused' && (
          <Button size="s" onClick={() => void flows.resume(d)} aria-label={t('models.dl.resume_label', { model: d.model })}>
            {t('models.dl.action.resume')}
          </Button>
        )}
        {(d.state === 'running' || d.state === 'paused') && (
          <Button size="s" onClick={onCancel} aria-label={t('models.dl.cancel_label', { model: d.model })}>
            {t('models.dl.action.cancel')}
          </Button>
        )}
        {d.state === 'queued' && (
          <Button size="s" variant="text" onClick={() => void flows.remove(d, false)}>
            {t('models.dl.action.remove')}
          </Button>
        )}
        {d.state === 'done' && (
          <>
            <Button size="s" variant={engineStopped ? 'solid' : 'outline'} onClick={() => void flows.loadModel(d.model)}>
              {t('models.action.load')}
            </Button>
            <Button size="s" variant="text" onClick={onDismiss}>
              {t('models.dl.action.dismiss')}
            </Button>
          </>
        )}
        {d.state === 'cancelled' && !err && (
          <Button size="s" variant="text" onClick={() => void flows.remove(d, true)}>
            {t('models.dl.action.remove')}
          </Button>
        )}
        {err?.actions.map(act)}
      </div>
      {files.length > 0 && (
        <Disclosure summary={t('models.dl.files', { n: files.length })}>
          <ul class="dl-files">
            {files.map((f) => {
              const p = typeof f.size_bytes === 'number' && f.size_bytes > 0 ? f.done_bytes / f.size_bytes : f.state === 'done' ? 1 : 0;
              return (
                <li key={`${f.repo_id}/${f.name}`} class="dl-file">
                  <span class="mono">{f.repo_id !== d.model.split(':')[0] ? `${f.repo_id} ${f.name}` : f.name}</span>
                  <span class="tnum">{formatBytes(f.size_bytes, HUB)}</span>
                  <span class="meta tnum">
                    {f.state === 'done' ? t('models.dl.file_done') : f.state === 'pending' ? t('models.dl.file_pending') : percent(p)}
                  </span>
                  {f.state === 'downloading' && <ProgressBar value={p} label={t('models.dl.item_progress', { model: f.name })} />}
                </li>
              );
            })}
          </ul>
        </Disclosure>
      )}
      <details class="disclosure" open={logOpen} onToggle={(e) => setLogOpen((e.currentTarget as HTMLDetailsElement).open)}>
        <summary class="label">
          <span class="disclosure-glyph" aria-hidden="true">
            ▸
          </span>
          {t('models.dl.log')}
        </summary>
        <div class="disclosure-body">
          {logOpen && <LogPane lines={log.map((text, i) => ({ key: i, text }))} label={t('models.dl.log_label', { model: d.model })} height={200} empty={t('models.dl.log_empty')} />}
        </div>
      </details>
    </li>
  );
}
