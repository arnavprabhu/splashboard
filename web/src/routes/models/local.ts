/**
 * Local GGUF drop-in (SPEC §9.6, D49): a `.gguf` dropped into the models folder becomes
 * `local/<stem>-GGUF`. Kept out of ./api and ./logic, which the initial Status bundle shares.
 */

import { ADMIN, request } from '../../api/client';

/** One `GET /models/local` outcome: the model the file became, or why it was not added. */
export type LocalOutcome = { model: string; family?: string; draft?: string; language_only?: boolean } | { error: string };

export interface LocalView {
  files: Record<string, LocalOutcome>;
  ignored: string[];
}

export function isLocalId(id: string): boolean {
  return id.startsWith('local/');
}

/** Files that were not added, by file name, with Splash's reason. */
export function localErrors(view: LocalView | null): { file: string; error: string }[] {
  if (!view) return [];
  return Object.entries(view.files)
    .flatMap(([file, o]) => ('error' in o && typeof o.error === 'string' ? [{ file, error: o.error }] : []))
    .sort((a, b) => a.file.localeCompare(b.file));
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

function readLocal(v: unknown): LocalView {
  const files: LocalView['files'] = {};
  if (isRecord(v) && isRecord(v.files)) {
    for (const [name, o] of Object.entries(v.files)) if (isRecord(o)) files[name] = o as LocalOutcome;
  }
  const ignored = isRecord(v) && Array.isArray(v.ignored) ? v.ignored.filter((k): k is string => typeof k === 'string') : [];
  return { files, ignored };
}

/** GET /models/local: loose `.gguf` files and what became of each. */
export function getLocal(signal?: AbortSignal): Promise<LocalView> {
  return request<unknown>(`${ADMIN}/models/local`, signal ? { signal } : {}).then(readLocal);
}

/** POST /models/local/rescan: look for dropped files now; `added` lists the new model IDs. */
export async function rescanLocal(restoreIgnored = false): Promise<LocalView & { added: string[] }> {
  const body = await request<unknown>(`${ADMIN}/models/local/rescan`, { method: 'POST', ...(restoreIgnored ? { query: { restore_ignored: true } } : {}) });
  const added = isRecord(body) && Array.isArray(body.added) ? body.added.filter((id): id is string => typeof id === 'string') : [];
  return { ...readLocal(body), added };
}
