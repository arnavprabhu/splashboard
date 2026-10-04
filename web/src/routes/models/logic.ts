/**
 * Pure helpers for the Models pages (docs/ui/03-models.md): labels, sorting, the delete
 * threshold, download summaries, error mapping, ID validation and the lazy-check limiter.
 * No DOM, no fetch: everything here is unit-tested in tests/models-logic.test.ts.
 */

import type { Catalog, CatalogEntry, DownloadItem, InspectResult, InstalledModel, VariantOut } from '../../api/models';
import { DASH, formatBytes, formatIndex } from '../../lib/format';
import { modelIdError, splitModelId } from '../../lib/model-id';
import { t } from '../../strings/models';

export const GIB = 1024 ** 3;
/** Typed confirmation at ≥ 10 GB (decision F4). */
export const TYPED_DELETE_BYTES = 10 * GIB;
/** SPEC §9.4: free space must cover the remaining bytes plus a 2 GB margin. */
export const DISK_MARGIN_BYTES = 2 * GIB;
/** Search results: at most this many /inspect calls in flight (decision M9). */
export const MAX_LAZY_CHECKS = 3;
/** /inspect runs Splash's helper with a 20 s timeout; the UI waits as long (03 §3.3). */
export const INSPECT_TIMEOUT_MS = 20_000;

export const ACTIVE_DOWNLOAD_STATES: ReadonlySet<DownloadItem['state']> = new Set(['queued', 'running', 'paused', 'verifying']);

// ---------- names and labels ----------

/** Repo name without the owner, variant kept: `unsloth/X-GGUF:UD-Q4_K_M` → `X-GGUF:UD-Q4_K_M`. */
export function shortName(id: string): string {
  const slash = id.indexOf('/');
  return slash < 0 ? id : id.slice(slash + 1);
}

/** Repo part of an ID (no variant). */
export function repoOf(id: string): string {
  return splitModelId(id).repo;
}

export function hfUrl(id: string): string {
  return `https://huggingface.co/${repoOf(id)}`;
}

export function sha7(commit: string | null | undefined): string | null {
  return commit ? commit.slice(0, 7) : null;
}

/** Splash's legacy packages: `incoai/*-Splash` (SPEC §3.2). */
export function isLegacyId(id: string): boolean {
  return /^incoai\/[^/:]+-Splash$/i.test(repoOf(id));
}

/** Clef fine-tunes load but are not Clef-accurate (SPEC Appendix D.2). */
export function isClefId(id: string): boolean {
  return /clef/i.test(id);
}

export type FormatKind = 'mlx' | 'gguf' | 'pq2' | 'legacy' | 'unknown';

export function formatKind(format: string | null | undefined, id = ''): FormatKind {
  const variant = splitModelId(id).variant ?? '';
  if (format === 'pq2' || /^PQ2/i.test(variant)) return 'pq2';
  if (format === 'mlx' || format === 'gguf' || format === 'legacy') return format;
  if (!id) return 'unknown';
  // Not known yet (a download that is not installed): guess from the ID.
  if (isLegacyId(id)) return 'legacy';
  if (/gguf/i.test(id)) return 'gguf';
  if (/mlx|4bit/i.test(id)) return 'mlx';
  return 'unknown';
}

/** `MLX 4-bit`, `GGUF`, `GGUF PQ2_0`, `Splash pkg` (03 §1.3; uppercased by CSS). */
export function formatLabel(format: string | null | undefined, id = ''): string {
  return t(`models.format.${formatKind(format, id)}`);
}

/** `01 — MLX 4-bit · 21 GB` (03 §1.3). Disk sizes are base 1024. */
export function rowLabel(index: number, format: string | null | undefined, id: string, bytes: number | null | undefined, base: 1024 | 1000 = 1024): string {
  const size = bytes === null || bytes === undefined ? DASH : formatBytes(bytes, { base });
  return `${formatIndex(index)} — ${formatLabel(format, id)} · ${size}`;
}

/** Installed rows: last used first, then by ID (03 §1.3). */
export function sortInstalled<T extends Pick<InstalledModel, 'id' | 'last_used_at'>>(list: readonly T[]): T[] {
  const at = (m: T) => (m.last_used_at ? Date.parse(m.last_used_at) : NaN);
  return [...list].sort((a, b) => {
    const ta = at(a);
    const tb = at(b);
    const fa = Number.isFinite(ta);
    const fb = Number.isFinite(tb);
    if (fa && fb && ta !== tb) return tb - ta;
    if (fa !== fb) return fa ? -1 : 1;
    return a.id.localeCompare(b.id);
  });
}

// ---------- delete ----------

export interface DeletePlan {
  ids: string[];
  /** Sum of `unique_bytes`: what disappears from disk (ref-counted by the manager). */
  freesBytes: number;
  totalBytes: number;
  includesActive: boolean;
  typed: boolean;
  /** Drafts that stay because another installed model shares them. */
  keptDrafts: string[];
}

export function deletePlan(models: readonly InstalledModel[], activeId: string | null, all = false): DeletePlan {
  const freesBytes = models.reduce((s, m) => s + (m.unique_bytes ?? m.size_bytes ?? 0), 0);
  const totalBytes = models.reduce((s, m) => s + (m.size_bytes ?? 0), 0);
  const keptDrafts = [...new Set(models.filter((m) => m.draft?.shared).map((m) => m.draft!.repo_id))];
  return {
    ids: models.map((m) => m.id),
    freesBytes,
    totalBytes,
    includesActive: activeId !== null && models.some((m) => m.id === activeId),
    typed: all || freesBytes >= TYPED_DELETE_BYTES,
    keptDrafts,
  };
}

// ---------- downloads ----------

export interface DownloadSummary {
  active: number;
  running: number;
  queued: number;
  paused: number;
  verifying: number;
  doneBytes: number;
  totalBytes: number;
  leftBytes: number | null;
  speedBps: number | null;
  etaS: number | null;
  /** 0–1 across active and queued items, null when nothing is active. */
  progress: number | null;
}

export function summarizeDownloads(items: readonly DownloadItem[]): DownloadSummary {
  const active = items.filter((d) => ACTIVE_DOWNLOAD_STATES.has(d.state));
  const count = (s: DownloadItem['state']) => active.filter((d) => d.state === s).length;
  const known = active.every((d) => typeof d.bytes_total === 'number' && d.bytes_total > 0);
  const totalBytes = active.reduce((s, d) => s + (d.bytes_total ?? 0), 0);
  const doneBytes = active.reduce((s, d) => s + (d.bytes_done ?? 0), 0);
  const running = active.filter((d) => d.state === 'running');
  const speeds = running.map((d) => d.speed_bps).filter((v): v is number => typeof v === 'number');
  const speedBps = speeds.length ? speeds.reduce((a, b) => a + b, 0) : null;
  const leftBytes = known && active.length ? Math.max(0, totalBytes - doneBytes) : null;
  const etas = running.map((d) => d.eta_s).filter((v): v is number => typeof v === 'number');
  const etaS = leftBytes !== null && speedBps ? leftBytes / speedBps : etas.length ? Math.max(...etas) : null;
  let progress: number | null = null;
  if (active.length) {
    if (totalBytes > 0) progress = Math.min(1, doneBytes / totalBytes);
    else progress = active.reduce((s, d) => s + (d.progress ?? 0), 0) / active.length;
  }
  return {
    active: active.length,
    running: running.length,
    queued: count('queued'),
    paused: count('paused'),
    verifying: count('verifying'),
    doneBytes,
    totalBytes,
    leftBytes,
    speedBps,
    etaS,
    progress,
  };
}

/** 0–1 for one item: bytes when known, else `progress`. */
export function itemProgress(d: Pick<DownloadItem, 'bytes_done' | 'bytes_total' | 'progress'>): number | null {
  if (typeof d.bytes_total === 'number' && d.bytes_total > 0) return Math.min(1, (d.bytes_done ?? 0) / d.bytes_total);
  return typeof d.progress === 'number' ? d.progress : null;
}

/** `42%` (floor, so 99.6% never reads 100% before the download is done). */
export function percent(ratio: number | null | undefined): string {
  return typeof ratio === 'number' && Number.isFinite(ratio) ? `${Math.floor(Math.min(1, Math.max(0, ratio)) * 100)}%` : DASH;
}

export type DownloadAction = 'add_token' | 'storage' | 'retry' | 'work_offline' | 'details' | 'remove' | 'full_verify' | 'redownload' | 'resume';

export interface DownloadErrorView {
  text: string;
  actions: DownloadAction[];
}

/** Every DownloadError code → row text and actions (03 §3.5 error table, api.md §7). */
export function downloadErrorView(item: Pick<DownloadItem, 'error'>, installed: boolean): DownloadErrorView | null {
  const e = item.error;
  if (!e) return null;
  const message = e.message ?? '';
  switch (e.code) {
    case 'gated':
      return { text: t('models.dl.error.gated'), actions: ['add_token', 'retry', 'remove'] };
    case 'disk_full':
      return {
        text:
          typeof e.needed_bytes === 'number' && typeof e.free_bytes === 'number'
            ? t('models.dl.error.disk_full', { needed: formatBytes(e.needed_bytes), free: formatBytes(e.free_bytes) })
            : t('models.dl.error.disk_full_plain'),
        actions: ['storage', 'retry', 'remove'],
      };
    case 'hub_unreachable':
      return { text: t('models.dl.error.hub_unreachable'), actions: installed ? ['retry', 'work_offline', 'remove'] : ['retry', 'remove'] };
    case 'incompatible':
      return { text: t('models.dl.error.incompatible', { message }), actions: ['details', 'remove'] };
    case 'verify_failed':
      return { text: t('models.dl.error.verify_failed', { message }), actions: ['full_verify', 'redownload', 'remove'] };
    case 'cancelled':
      return { text: t('models.dl.error.interrupted'), actions: ['resume', 'remove'] };
    case 'installer_failed':
    default: {
      const actions: DownloadAction[] = ['retry', 'details', 'remove'];
      if (e.action === 'add_hf_token') actions.unshift('add_token');
      if (e.action === 'free_space') actions.unshift('storage');
      if (e.action === 'go_offline' && installed) actions.splice(1, 0, 'work_offline');
      return { text: t('models.dl.error.failed', { message }), actions };
    }
  }
}

// ---------- download by ID ----------

export interface IdCheck {
  error: string | null;
  /** Extra hint for short names (`qwen3.8-27B`), which the old zsh function used. */
  hint: string | null;
}

export function checkModelId(value: string): IdCheck {
  const v = value.trim();
  if (!v) return { error: null, hint: null };
  const error = modelIdError(v);
  if (!error) return { error: null, hint: null };
  return { error: t('models.byid.invalid'), hint: v.includes('/') ? null : t('models.byid.short_hint') };
}

/** The variant to preselect: the one named in the ID, else the recommended loadable one. */
export function pickVariant(result: Pick<InspectResult, 'variants' | 'recommended_variant'> | null, requested: string | null): string | null {
  const variants = result?.variants ?? [];
  if (!variants.length) return null;
  const loadable = (v: VariantOut) => v.loadable !== false;
  if (requested) {
    const hit = variants.find((v) => v.name.toLowerCase() === requested.toLowerCase());
    if (hit) return hit.name;
  }
  const rec = variants.find((v) => v.name === result?.recommended_variant && loadable(v)) ?? variants.find((v) => v.recommended && loadable(v));
  return (rec ?? variants.find(loadable))?.name ?? null;
}

/** `mmproj-*` files are vision projectors, not variants (SPEC §9.1). */
export function isProjector(name: string): boolean {
  return /^mmproj/i.test(name);
}

export interface DiskCheck {
  ok: boolean;
  neededBytes: number;
  freeBytes: number | null;
}

export function diskCheck(bytes: number | null | undefined, freeBytes: number | null | undefined): DiskCheck {
  const neededBytes = (bytes ?? 0) + DISK_MARGIN_BYTES;
  if (typeof freeBytes !== 'number' || typeof bytes !== 'number') return { ok: true, neededBytes, freeBytes: freeBytes ?? null };
  return { ok: neededBytes <= freeBytes, neededBytes, freeBytes };
}

// ---------- titles ----------

/** `42% · Models` while a download runs (docs/ui/01 §10); the shell appends " — Splash GUI". */
export function pageTitle(page: string, progress: number | null): string {
  return progress === null ? page : `${percent(progress)} · ${page}`;
}

// ---------- lazy compatibility checks (decision M9) ----------

export interface Limiter {
  /** Queues `task` under `key`; returns a cancel function (a task that has not started is skipped). */
  run(key: string, task: () => Promise<unknown>): () => void;
  readonly inFlight: number;
  readonly queued: number;
}

export function createLimiter(max: number): Limiter {
  let inFlight = 0;
  const queue: Array<{ key: string; task: () => Promise<unknown>; cancelled: boolean }> = [];
  const pump = () => {
    while (inFlight < max && queue.length) {
      const job = queue.shift()!;
      if (job.cancelled) continue;
      inFlight += 1;
      void job
        .task()
        .catch(() => undefined)
        .finally(() => {
          inFlight -= 1;
          pump();
        });
    }
  };
  return {
    run(key, task) {
      const job = { key, task, cancelled: false };
      queue.push(job);
      pump();
      return () => {
        job.cancelled = true;
      };
    },
    get inFlight() {
      return inFlight;
    },
    get queued() {
      return queue.filter((j) => !j.cancelled).length;
    },
  };
}

// ---------- catalog ----------

/** The catalog's single "Recommended for this Mac" entry (exactly one, 03 §3.2). */
export function recommendedEntry(catalog: Catalog | null): CatalogEntry | null {
  for (const f of catalog?.families ?? []) for (const g of f.groups) for (const e of g.entries) if (e.recommended) return e;
  return null;
}

/**
 * SPEC Appendix C, shown when GET /catalog is unavailable so the list is never blank. Sizes,
 * fit and variants come from the manager; without it they read "—".
 */
export function catalogSeed(memoryBytes = 0): Catalog {
  const entry = (id: string, family: CatalogEntry['family'], format: CatalogEntry['format'], notes: string): CatalogEntry => ({
    id,
    repo_id: repoOf(id),
    family,
    format,
    notes,
    installed: false,
    recommended: false,
  });
  return {
    memory_bytes: memoryBytes,
    offline: true,
    families: [
      {
        family: 'Qwen3.8-27B',
        label: t('models.catalog.family.dense'),
        groups: [
          { format: 'mlx', label: t('models.format.mlx'), entries: [entry('mlx-community/Qwen3.8-27B-4bit', 'Qwen3.8-27B', 'mlx', t('models.seed.dense_mlx'))] },
          { format: 'gguf', label: t('models.format.gguf'), entries: [entry('unsloth/Qwen3.8-27B-GGUF', 'Qwen3.8-27B', 'gguf', t('models.seed.dense_gguf'))] },
          { format: 'pq2', label: t('models.format.pq2'), entries: [entry('prism-ml/Ternary-Bonsai-2-27B-gguf:PQ2_0', 'Qwen3.8-27B', 'gguf', t('models.seed.pq2'))] },
          { format: 'legacy', label: t('models.format.legacy'), entries: [entry('incoai/Qwen3.8-27B-Splash', 'Qwen3.8-27B', 'legacy', t('models.seed.legacy'))] },
        ],
      },
      {
        family: 'Qwen3.6-35B-A3B',
        label: t('models.catalog.family.moe'),
        groups: [
          { format: 'mlx', label: t('models.format.mlx'), entries: [entry('mlx-community/Qwen3.6-35B-A3B-4bit', 'Qwen3.6-35B-A3B', 'mlx', t('models.seed.moe_mlx'))] },
          { format: 'gguf', label: t('models.format.gguf'), entries: [entry('unsloth/Qwen3.6-35B-A3B-GGUF', 'Qwen3.6-35B-A3B', 'gguf', t('models.seed.moe_gguf'))] },
          { format: 'legacy', label: t('models.format.legacy'), entries: [entry('incoai/Qwen3.6-35B-A3B-Splash', 'Qwen3.6-35B-A3B', 'legacy', t('models.seed.legacy'))] },
        ],
      },
    ],
  };
}

/** Size span of a GGUF repo's loadable variants: `11–29 GB`. */
export function variantSpan(variants: readonly VariantOut[] | null | undefined, base: 1024 | 1000 = 1000): string | null {
  const sizes = (variants ?? []).filter((v) => !isProjector(v.name) && v.loadable !== false && typeof v.size_bytes === 'number').map((v) => v.size_bytes!);
  if (!sizes.length) return null;
  const lo = Math.min(...sizes);
  const hi = Math.max(...sizes);
  if (lo === hi) return formatBytes(lo, { base });
  const [loNum] = formatBytes(lo, { base }).split(' ');
  return `${loNum}–${formatBytes(hi, { base })}`;
}

/** Installed variants of a repo, from the installed list. */
export function installedVariants(repo: string, installed: readonly Pick<InstalledModel, 'id'>[]): Set<string> {
  const out = new Set<string>();
  for (const m of installed) {
    const { repo: r, variant } = splitModelId(m.id);
    if (r.toLowerCase() === repo.toLowerCase()) out.add(variant ?? '');
  }
  return out;
}
