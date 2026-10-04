/**
 * Shared user flows with their toasts (docs/ui/03 §1.4, §4): load, start a download, reveal,
 * pause/resume/remove a download. Pages call these; inline errors are left to the caller by
 * rethrowing where the page shows them in place (507, 400 incompatible).
 */

import { ApiError } from '../../api/client';
import type { DownloadItem } from '../../api/models';
import { toast, toastError } from '../../components/Toast';
import { formatBytes } from '../../lib/format';
import { refreshDownloads, upsertDownload } from '../../store';
import { t } from '../../strings/models';
import * as models from './api';
import { shortName } from './logic';

export async function loadModel(id: string): Promise<boolean> {
  try {
    await models.loadEngine(id);
    toast(t('models.toast.loading', { model: shortName(id) }));
    return true;
  } catch (err) {
    if (err instanceof ApiError && err.code === 'model_switch_busy') toast(t('models.toast.busy'), { tone: 'error', detail: err.message });
    else toastError(t('models.toast.load_failed', { short: shortName(id) }), err);
    return false;
  }
}

export async function unloadModel(): Promise<boolean> {
  try {
    await models.stopEngine();
    toast(t('models.toast.stopping'));
    return true;
  } catch (err) {
    toastError(t('models.toast.stop_failed'), err);
    return false;
  }
}

export async function revealModel(id: string): Promise<void> {
  try {
    await models.revealModel(id);
  } catch (err) {
    toastError(t('models.toast.reveal_failed'), err);
  }
}

export function scrollToDownloads(): void {
  requestAnimationFrame(() => document.getElementById('downloads')?.scrollIntoView({ block: 'nearest' }));
}

/**
 * POST /downloads (03 §4). On success the item enters the store, the queue scrolls into view
 * and a toast confirms. 409 already_queued is a toast; 507 and 400 rethrow for inline display.
 */
export async function startDownload(req: models.StartDownload, sizeBytes?: number | null): Promise<DownloadItem | null> {
  try {
    const item = await models.postDownload(req);
    if (item && typeof item === 'object' && typeof item.id === 'string') upsertDownload({ files: [], log_tail: [], ...item });
    void refreshDownloads();
    const size = typeof sizeBytes === 'number' ? formatBytes(sizeBytes, { base: 1000 }) : null;
    toast(size ? t('models.toast.queued_size', { model: shortName(req.id), size }) : t('models.toast.queued', { model: shortName(req.id) }));
    scrollToDownloads();
    return item;
  } catch (err) {
    if (err instanceof ApiError && err.code === 'already_queued') {
      toast(t('models.toast.already_queued', { short: shortName(req.id) }));
      scrollToDownloads();
      return null;
    }
    if (err instanceof ApiError && (err.status === 507 || err.status === 400 || err.status === 422)) throw err;
    toastError(t('models.toast.download_failed', { short: shortName(req.id) }), err);
    return null;
  }
}

async function withRefresh<T>(fn: () => Promise<T>, failure: string): Promise<T | null> {
  try {
    const out = await fn();
    if (out && typeof out === 'object' && typeof (out as { id?: unknown }).id === 'string' && 'state' in (out as object)) {
      upsertDownload({ files: [], log_tail: [], ...(out as unknown as DownloadItem) });
    }
    void refreshDownloads();
    return out;
  } catch (err) {
    toastError(failure, err);
    void refreshDownloads();
    return null;
  }
}

export function pause(item: DownloadItem) {
  return withRefresh(() => models.pauseDownload(item.id), t('models.toast.pause_failed'));
}

export function resume(item: DownloadItem) {
  return withRefresh(() => models.resumeDownload(item.id), t('models.toast.resume_failed'));
}

export function remove(item: DownloadItem, keepFiles: boolean) {
  return withRefresh(() => models.removeDownload(item.id, keepFiles), t('models.toast.remove_failed'));
}

/** Retry = a fresh POST with the same options; the failed row is then removed (files kept). */
export async function retry(item: DownloadItem): Promise<void> {
  const req: models.StartDownload = { id: item.model, revision: item.revision ?? null, draft_model: item.draft_model ?? null, language_only: item.language_only };
  try {
    await startDownload(req, item.bytes_total ?? null);
  } catch (err) {
    toastError(t('models.toast.download_failed', { short: shortName(item.model) }), err);
    return;
  }
  try {
    await models.removeDownload(item.id, true);
  } catch {
    /* the old row stays; harmless */
  }
  void refreshDownloads();
}
