import { api } from '../../api/client';
import { loadSettings, settings } from '../../store';

/** Reads one global setting from the cached settings document. */
export function globalSetting<T>(section: string, key: string, fallback: T): T {
  const v = settings.value?.settings.global[section]?.[key];
  return v === undefined || v === null ? fallback : (v as T);
}

/**
 * Writes one global setting immediately (`downloads.parallel`, `hf.offline`): GET the whole
 * document, change the key, PUT it back (api.md §6.1 replaces the whole document).
 */
export async function writeGlobalSetting(section: string, key: string, value: unknown): Promise<void> {
  const current = await loadSettings(true);
  if (!current) throw new Error('Settings are unavailable');
  const doc = structuredClone(current.settings);
  doc.global[section] = { ...(doc.global[section] ?? {}), [key]: value };
  await api.put('/settings', doc);
  await loadSettings(true);
}
