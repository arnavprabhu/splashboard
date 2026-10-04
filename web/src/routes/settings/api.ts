/**
 * Typed adapters for the settings routes (docs/api.md §6, §9, §12.3, §2). Pages call these
 * instead of `api.*` directly so a shape change lands in one place.
 */

import { api, request } from '../../api/client';
import type {
  DataClearResult,
  DataSizes,
  DoctorReport,
  EffectiveSettings,
  HfTokenTestOut,
  LaunchPreview,
  McpServer,
  ModelDetail,
  ProfilesView,
  SamplingOverlay,
  SecretsState,
  SettingsSaveResult,
  SettingsSchema,
  SettingsValidation,
} from '../../api/models';
import type { SettingsDoc } from '../../api/types';

/** Model IDs go into paths literally (docs/api.md §1.1); each segment is encoded. */
export function modelPath(id: string): string {
  return id
    .split('/')
    .map((part) => encodeURIComponent(part).replace(/%3A/gi, ':'))
    .join('/');
}

export interface SettingsEnvelope {
  settings: SettingsDoc;
  secrets: SecretsState;
  resolved: Record<string, string>;
  read_only: boolean;
  load_warnings: string[];
}

export function readEnvelope(body: unknown): SettingsEnvelope {
  const b = (body ?? {}) as Partial<SettingsEnvelope> & { settings?: SettingsDoc };
  const settings = b.settings ?? ({ version: 1, global: {}, models: {} } as SettingsDoc);
  return {
    settings: { version: settings.version ?? 1, global: settings.global ?? {}, models: settings.models ?? {} },
    secrets: b.secrets ?? { api_key_set: false, hf_token_override_set: false, hf_login_token_present: false },
    resolved: (b.resolved as Record<string, string> | undefined) ?? {},
    read_only: Boolean(b.read_only),
    load_warnings: Array.isArray(b.load_warnings) ? b.load_warnings : [],
  };
}

export const settingsApi = {
  get: async (signal?: AbortSignal) => readEnvelope(await api.get<unknown>('/settings', undefined, signal)),
  schema: (signal?: AbortSignal) => api.get<SettingsSchema>('/settings/schema', undefined, signal),
  effective: (model: string | null, signal?: AbortSignal) =>
    api.get<EffectiveSettings>('/settings/effective', model ? { model } : undefined, signal),
  validate: (doc: SettingsDoc, signal?: AbortSignal) => api.post<SettingsValidation>('/settings/validate', doc, signal),
  save: (doc: SettingsDoc) => api.put<SettingsSaveResult>('/settings', doc),
  launchPreview: (model: string, signal?: AbortSignal) => api.get<LaunchPreview>('/settings/launch-preview', { model }, signal),

  revealApiKey: () => api.get<{ key: string | null }>('/settings/secrets/api-key'),
  generateApiKey: () => api.post<{ key: string }>('/settings/secrets/api-key'),
  deleteApiKey: () => api.del<void>('/settings/secrets/api-key'),
  setHfToken: (token: string) => api.put<SecretsState>('/settings/secrets/hf-token', { token }),
  deleteHfToken: () => api.del<void>('/settings/secrets/hf-token'),
  testHfToken: (token?: string) => api.post<HfTokenTestOut>('/settings/secrets/hf-token/test', token ? { token } : {}),

  profiles: (model: string, signal?: AbortSignal) => api.get<ProfilesView>(`/models/${modelPath(model)}/profiles`, undefined, signal),
  saveProfiles: (model: string, body: { profiles?: Record<string, SamplingOverlay | null>; sampling_defaults?: SamplingOverlay }) =>
    api.put<ProfilesView>(`/models/${modelPath(model)}/profiles`, body),
  model: (model: string, signal?: AbortSignal) => api.get<ModelDetail>(`/models/${modelPath(model)}`, undefined, signal),
  models: (signal?: AbortSignal) => api.get<{ models: Array<{ id: string; legacy?: boolean; status?: string }> }>('/models', undefined, signal),

  mcpServers: (signal?: AbortSignal) => api.get<{ servers: Record<string, McpServer> }>('/mcp/servers', undefined, signal),
  saveMcpServers: (servers: Record<string, McpServer>) => api.put<{ servers: Record<string, McpServer> }>('/mcp/servers', { servers }),
  mcpTools: (signal?: AbortSignal) =>
    api.get<{ tools: Array<{ server: string; name: string }>; errors: Array<{ server: string; message: string }> }>('/mcp/tools', undefined, signal),

  dataSizes: (signal?: AbortSignal) => api.get<DataSizes>('/data/sizes', undefined, signal),
  clearData: (target: string) => api.post<DataClearResult>('/data/clear', { target }),
  traces: (signal?: AbortSignal) =>
    api.get<{ enabled: boolean; directory: string; traces: Array<{ name: string; size_bytes: number }> }>('/traces', undefined, signal),
  deleteTrace: (name: string) => api.del<void>(`/traces/${encodeURIComponent(name)}`),

  restartEngine: () => api.post<unknown>('/engine/restart'),
  stopEngine: () => api.post<unknown>('/engine/stop'),
  upgradeEngine: () => api.post<{ job_id: string; kind: string }>('/engine/upgrade'),
  doctor: (signal?: AbortSignal) => api.get<DoctorReport>('/doctor', undefined, signal),
  reveal: (target: string, id: string | null = null) => api.post<unknown>('/system/reveal', { target, id }),
  moveStorage: (target: 'models' | 'cache', path: string) => api.post<unknown>('/storage/move', { target, path, move_files: true }),
};

/**
 * Probes the manager at a new origin after a rebind. Cross-origin `/health` has no CORS
 * headers, so the probe uses `no-cors`: an opaque answer means something is listening.
 */
export async function probeOrigin(origin: string, timeoutMs = 1500): Promise<boolean> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const sameOrigin = origin === location.origin;
    if (sameOrigin) await request('/health', { method: 'GET', signal: ctrl.signal });
    else await fetch(`${origin}/health`, { mode: 'no-cors', cache: 'no-store', signal: ctrl.signal });
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}
