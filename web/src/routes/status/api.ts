/**
 * Typed calls the Status and Usage history pages make (docs/api.md §3, §5, §7, §10). Shapes come
 * from the generated OpenAPI models; where the page needs more than the contract gives it says
 * so next to the call.
 */

import { ADMIN, api, buildUrl, type Query } from '../../api/client';
import type {
  EngineView,
  InstalledModels,
  MetricsSeries,
  OkResponse,
  ProfilesView,
  SettingsSaveResult,
  UsageRows,
  UsageSummary,
  UsageTimeseries,
} from '../../api/models';
import type { SettingsDoc } from '../../api/types';
import { loadSettings } from '../../store';
import { applyPatch } from './logic';

// ---------- engine ----------

export const loadModel = (model: string, force = false) => api.post<EngineView>('/engine/load', { model, force });
export const stopEngine = () => api.post<EngineView>('/engine/stop');
export const restartEngine = () => api.post<EngineView>('/engine/restart');
/** Raw Splash /status (schema 6, any shape); 503 while the engine is not running. */
export const rawStatus = (signal?: AbortSignal) => api.get<Record<string, unknown>>('/engine/status', undefined, signal);

// ---------- metrics ----------

export const metricsSeries = (windowS: number, signal?: AbortSignal) => api.get<MetricsSeries>('/metrics/series', { window: windowS }, signal);
export const resetMetrics = () => api.post<OkResponse>('/metrics/reset');

// ---------- models ----------

export const listModels = (signal?: AbortSignal) => api.get<InstalledModels>('/models', undefined, signal);
/** Model IDs go into the path literally (docs/api.md §1.1). */
export const modelProfiles = (id: string, signal?: AbortSignal) => api.get<ProfilesView>(`/models/${id}/profiles`, undefined, signal);

// ---------- secrets ----------

/** Reveals the API key (admin session only, docs/api.md §6.5). */
export async function fetchApiKey(): Promise<string | null> {
  const body = await api.get<{ key?: string | null }>('/settings/secrets/api-key');
  return body?.key ?? null;
}

// ---------- settings ----------

/** Reads the current document, applies a suggestion patch and saves it (PUT /settings). */
export async function savePatch(patch: Record<string, unknown>, model: string | null): Promise<SettingsSaveResult> {
  const current = await loadSettings(true);
  if (!current) throw new Error('Settings are not available');
  const doc = applyPatch(current.settings as SettingsDoc, patch, model);
  const result = await api.put<SettingsSaveResult>('/settings', doc);
  void loadSettings(true);
  return result;
}

// ---------- usage ----------

export interface UsageFilterQuery extends Query {
  start?: string;
  end?: string;
  model?: string;
  endpoint?: string;
  status?: string;
  client?: string;
}

/**
 * GET /usage/summary. The contract takes `scope` and `model` only; the history page also sends
 * `start`/`end`/`endpoint`/`status`/`client` (an API request in the report) and the manager
 * ignores what it does not know.
 */
export const usageSummary = (query: Query, signal?: AbortSignal) => api.get<UsageSummary>('/usage/summary', query, signal);
export const usageTimeseries = (query: Query, signal?: AbortSignal) => api.get<UsageTimeseries>('/usage/timeseries', query, signal);
export const usageRequests = (query: Query, signal?: AbortSignal) => api.get<UsageRows>('/usage/requests', query, signal);
export const exportCsvUrl = (query: UsageFilterQuery) => buildUrl(`${ADMIN}/usage/export.csv`, query);
