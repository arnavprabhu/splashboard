/**
 * Typed adapter for the Models pages (reveal, load/stop). Readers are
 * tolerant (missing fields get defaults) so a partial backend never crashes a page.
 */

import { api, ApiError } from '../../api/client';
import type {
  Catalog,
  DeleteModelResult,
  DiskUsage,
  DownloadItem,
  EngineView,
  HfWhoami,
  InspectResult,
  InstalledModel,
  JobAccepted,
  ModelCard,
  ModelDetail,
  OkResponse,
  SearchResults,
  StorageInfo,
} from '../../api/models';

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

/** Model IDs go into paths literally; each segment is encoded, `:` kept. */
export function modelPath(id: string): string {
  return id
    .split('/')
    .map((seg) => encodeURIComponent(seg).replace(/%3A/gi, ':'))
    .join('/');
}

export function readInstalled(v: unknown): InstalledModel | null {
  if (!isRecord(v) || typeof v.id !== 'string') return null;
  return {
    repo_id: v.id.split(':')[0]!,
    format: 'mlx',
    language_only: false,
    size_bytes: 0,
    unique_bytes: typeof v.size_bytes === 'number' ? v.size_bytes : 0,
    pinned: false,
    status: 'ready',
    ...v,
  } as InstalledModel;
}

export interface InstalledList {
  models: InstalledModel[];
  disk: DiskUsage | null;
}

export async function listModels(signal?: AbortSignal): Promise<InstalledList> {
  const body = await api.get<unknown>('/models', undefined, signal);
  const raw = isRecord(body) && Array.isArray(body.models) ? body.models : [];
  return {
    models: raw.map(readInstalled).filter((m): m is InstalledModel => m !== null),
    disk: isRecord(body) && isRecord(body.disk) ? (body.disk as unknown as DiskUsage) : null,
  };
}

/** GET /models/{id}; null when the model is not installed (404). */
export async function getModel(id: string, signal?: AbortSignal): Promise<ModelDetail | null> {
  try {
    const body = await api.get<unknown>(`/models/${modelPath(id)}`, undefined, signal);
    const base = readInstalled(body);
    return base ? ({ files: [], update_available: false, ...(body as object), ...base } as ModelDetail) : null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/** DELETE /models/{id}; `trashSource` (local/ models only) also moves the .gguf to the Trash. */
export function deleteModel(id: string, confirmActive: boolean, trashSource = false): Promise<DeleteModelResult> {
  const query: Record<string, true> = {};
  if (confirmActive) query.confirm_active = true;
  if (trashSource) query.trash_source = true;
  return api.del<DeleteModelResult>(`/models/${modelPath(id)}`, Object.keys(query).length ? query : undefined);
}

export function verifyModel(id: string, full: boolean): Promise<JobAccepted> {
  return api.post<JobAccepted>(`/models/${modelPath(id)}/verify`, { full });
}

export function updateModel(id: string): Promise<DownloadItem> {
  return api.post<DownloadItem>(`/models/${modelPath(id)}/update`);
}

export function getCatalog(signal?: AbortSignal): Promise<Catalog> {
  return api.get<Catalog>('/catalog', undefined, signal).then((c) => ({ ...c, families: Array.isArray(c?.families) ? c.families : [] }));
}

export type SearchSort = 'downloads' | 'likes' | 'recent';

export function searchHub(q: string, sort: SearchSort, signal?: AbortSignal): Promise<SearchResults> {
  return api
    .get<SearchResults>('/search', { q, sort, limit: 50 }, signal)
    .then((r) => ({ ...r, results: Array.isArray(r?.results) ? r.results : [] }));
}

export function readInspect(v: unknown): InspectResult | null {
  if (!isRecord(v) || typeof v.id !== 'string' || typeof v.badge !== 'string') return null;
  return { id: v.id, badge: v.badge as InspectResult['badge'], variants: [], vision: { available: false }, compatible: v.badge !== 'incompatible', cached: false, checked_at: '', repo_id: v.id.split(':')[0]!, ...v } as InspectResult;
}

export function getCard(id: string, signal?: AbortSignal): Promise<ModelCard> {
  return api.get<ModelCard>('/card', { id }, signal).then((c) => ({ ...c, markdown: typeof c?.markdown === 'string' ? c.markdown : '' }));
}

export interface StartDownload {
  id: string;
  revision?: string | null;
  draft_model?: string | null;
  language_only?: boolean;
}

export function postDownload(req: StartDownload): Promise<DownloadItem> {
  const body: Record<string, unknown> = { id: req.id, language_only: Boolean(req.language_only) };
  if (req.revision) body.revision = req.revision;
  if (req.draft_model) body.draft_model = req.draft_model;
  return api.post<DownloadItem>('/downloads', body);
}

export function pauseDownload(dl: string): Promise<DownloadItem> {
  return api.post<DownloadItem>(`/downloads/${encodeURIComponent(dl)}/pause`);
}

export function resumeDownload(dl: string): Promise<DownloadItem> {
  return api.post<DownloadItem>(`/downloads/${encodeURIComponent(dl)}/resume`);
}

/** DELETE /downloads/{dl}?keep_files= (Cancel's Keep/Delete). */
export function removeDownload(dl: string, keepFiles: boolean): Promise<void> {
  return api.del<void>(`/downloads/${encodeURIComponent(dl)}`, { keep_files: keepFiles });
}

export function revealModel(id: string): Promise<OkResponse> {
  return api.post<OkResponse>('/system/reveal', { target: 'model', id });
}

export function revealDir(target: 'models_dir' | 'cache_dir'): Promise<OkResponse> {
  return api.post<OkResponse>('/system/reveal', { target });
}

export function loadEngine(model: string, force = false): Promise<EngineView> {
  return api.post<EngineView>('/engine/load', force ? { model, force: true } : { model });
}

export function stopEngine(): Promise<EngineView> {
  return api.post<EngineView>('/engine/stop');
}

export function getStorage(signal?: AbortSignal): Promise<StorageInfo> {
  return api.get<StorageInfo>('/storage', undefined, signal);
}

/** Which token the manager would use and who it belongs to (read-only; nothing is saved). */
export function whoami(signal?: AbortSignal): Promise<HfWhoami> {
  return api.get<HfWhoami>('/hf/whoami', { use: 'active' }, signal);
}
