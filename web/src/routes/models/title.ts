import { DASH } from '../../lib/format';

/** `42%` (floor, so 99.6% never reads 100% before the download is done). */
export function percent(ratio: number | null | undefined): string {
  return typeof ratio === 'number' && Number.isFinite(ratio) ? `${Math.floor(Math.min(1, Math.max(0, ratio)) * 100)}%` : DASH;
}

/** `42% · Models` while a download runs; the shell appends " — Splashboard". */
export function pageTitle(page: string, progress: number | null): string {
  return progress === null ? page : `${percent(progress)} · ${page}`;
}
