/**
 * Typed calls the wizard makes (docs/api.md §2, §3, §6, §7, §12.4). Routes that may still be
 * stubs (501 `not_implemented`) are wrapped so the steps can show their designed fallback.
 * Missing routes are listed in the page report as API requests (POST /system/open-terminal,
 * POST /cli/install-path, `wizard.step` in settings).
 */

import { api, ApiError } from '../../api/client';
import type {
  BrewInfo,
  Catalog,
  DoctorReport,
  DownloadItem,
  DownloadList,
  DownloadRequest,
  EffectiveSettings,
  EngineView,
  ImportCandidates,
  InstalledModels,
  JobAccepted,
  PresetList,
  SettingsDocument,
  SettingsResponse,
  SettingsSaveResult,
  SettingsSchema,
  SettingsValidation,
  StorageInfo,
} from '../../api/models';
import { loadSettings } from '../../store';
import type { PresetId } from './steps';

/** A stub route (501) — the feature is not built into the manager yet. */
export function notBuilt(err: unknown): boolean {
  return err instanceof ApiError && err.status === 501;
}

/** Resolves to null when the route is still a stub; other errors propagate. */
async function orNull<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (err) {
    if (notBuilt(err)) return null;
    throw err;
  }
}

// ---------- system / engine ----------

export const getBrew = (signal?: AbortSignal) => api.get<BrewInfo>('/system/brew', undefined, signal);
/** null = the doctor is a stub. */
export const getDoctor = (signal?: AbortSignal) => orNull(api.get<DoctorReport>('/doctor', undefined, signal));
export const installEngine = () => api.post<JobAccepted>('/engine/install');
export const upgradeEngine = () => api.post<JobAccepted>('/engine/upgrade');
export const getEngine = () => api.get<EngineView>('/engine');
export const loadEngine = (model: string) => api.post<EngineView>('/engine/load', { model, force: false });

// ---------- settings ----------

export const getSettings = (signal?: AbortSignal) => api.get<SettingsResponse>('/settings', undefined, signal);
export const getPresets = (signal?: AbortSignal) => api.get<PresetList>('/settings/presets', undefined, signal);
export const getEffective = (signal?: AbortSignal) => api.get<EffectiveSettings>('/settings/effective', undefined, signal);
export const getSchema = (signal?: AbortSignal) => api.get<SettingsSchema>('/settings/schema', undefined, signal);
export const validateSettings = (doc: SettingsDocument, signal?: AbortSignal) => api.post<SettingsValidation>('/settings/validate', doc, signal);
export const applyPreset = (id: PresetId, model?: string) => api.post<SettingsSaveResult>(`/settings/presets/${id}/apply`, model ? { model } : {});
export const generateApiKey = () => api.post<{ key: string }>('/settings/secrets/api-key');
export const revealApiKey = () => api.get<{ key: string | null }>('/settings/secrets/api-key');
export const putHfToken = (token: string) => api.put<unknown>('/settings/secrets/hf-token', { token });

type Doc = SettingsDocument & { global: NonNullable<SettingsDocument['global']>; models: NonNullable<SettingsDocument['models']> };

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

/**
 * Read-modify-write of settings.json (PUT replaces the whole document, docs/api.md §6.1).
 * Reads a fresh copy first so other clients' edits are kept, then refreshes the store.
 */
export async function saveSettings(edit: (doc: Doc) => void): Promise<SettingsSaveResult> {
  const current = await getSettings();
  const doc = clone(current.settings) as Doc;
  doc.global ??= {};
  doc.models ??= {};
  edit(doc);
  const out = await api.put<SettingsSaveResult>('/settings', doc);
  await loadSettings(true);
  return out;
}

/** A settings document with one edit applied, for POST /settings/validate. */
export function withEdit(doc: SettingsDocument, edit: (doc: Doc) => void): SettingsDocument {
  const next = clone(doc) as Doc;
  next.global ??= {};
  next.models ??= {};
  edit(next);
  return next;
}

export async function markCompleted(extra?: (doc: Doc) => void): Promise<SettingsSaveResult> {
  return saveSettings((doc) => {
    doc.global.wizard = { ...(doc.global.wizard ?? { completed: false }), completed: true };
    extra?.(doc);
  });
}

// ---------- storage ----------

export const getStorage = (signal?: AbortSignal) => api.get<StorageInfo>('/storage', undefined, signal);
/** null = import is a stub. */
export const getImportCandidates = (signal?: AbortSignal) => orNull(api.get<ImportCandidates>('/storage/import-candidates', undefined, signal));
export const importModels = (repoIds: string[]) => api.post<JobAccepted>('/storage/import', { repo_ids: repoIds });

// ---------- models / downloads ----------

/** null = the catalog is a stub. */
export const getCatalog = (signal?: AbortSignal) => orNull(api.get<Catalog>('/catalog', undefined, signal));
/** null = the installed list is a stub. */
export const getInstalled = (signal?: AbortSignal) => orNull(api.get<InstalledModels>('/models', undefined, signal));
export const queueDownload = (body: DownloadRequest) => api.post<DownloadItem>('/downloads', body);
export const listDownloads = (signal?: AbortSignal) => api.get<DownloadList>('/downloads', undefined, signal);
export const pauseDownload = (id: string) => api.post<DownloadItem>(`/downloads/${encodeURIComponent(id)}/pause`);
export const resumeDownload = (id: string) => api.post<DownloadItem>(`/downloads/${encodeURIComponent(id)}/resume`);
export const cancelDownload = (id: string) => api.del<void>(`/downloads/${encodeURIComponent(id)}`, { keep_files: false });
