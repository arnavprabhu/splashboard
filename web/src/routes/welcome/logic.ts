/**
 * Pure logic for the wizard: step 1 checks and stop states, the step 3 preset diff
 * (decision W5), and the step 4 recommendation rows (SPEC §8.6, docs/api.md §6.5).
 */

import type {
  BrewInfo,
  Catalog,
  CatalogEntry,
  DoctorReport,
  DownloadItem,
  EffectiveSettings,
  EngineDiscoveryInfo,
  InstalledModel,
  PresetList,
  PresetOut,
  SettingsSchema,
  SystemInfo,
  VariantOut,
} from '../../api/models';
import { parseMaxCacheDisk, parseMaxContext } from '../../lib/size';
import type { EngineSummary } from '../../api/types';
import { isServing } from '../../lib/engine-state';
import { isActiveDownload } from '../../store';
import { splitModelId } from '../../lib/model-id';
import { t } from '../../strings/welcome';
import type { PresetId } from './steps';

// ---------- versions ----------

/** Compares dotted versions numerically ("26.4" < "27.0", "1.10" > "1.9"). */
export function compareVersions(a: string, b: string): number {
  const pa = a.split(/[.\-+ ]/).map((x) => parseInt(x, 10));
  const pb = b.split(/[.\-+ ]/).map((x) => parseInt(x, 10));
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = Number.isFinite(pa[i]) ? pa[i]! : 0;
    const y = Number.isFinite(pb[i]) ? pb[i]! : 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

export const MIN_MACOS = '26.4';

// ---------- step 1: stop states and checks ----------

export type StopKind = 'chip' | 'macos';

export interface StopReason {
  kind: StopKind;
  chip: string | null;
  macos: string;
  reasons: string[];
}

/**
 * SystemInfo.supported = M3+ and macOS ≥ 26.4 (docs/api.md §2). An old macOS gets its own stop
 * state (it can be fixed with Software Update); every other refusal is the chip one.
 */
export function stopReason(system: SystemInfo | null | undefined): StopReason | null {
  if (!system || system.supported) return null;
  const reasons = system.unsupported_reasons ?? [];
  const macosOld = compareVersions(system.macos_version, MIN_MACOS) < 0;
  const chipNamed = reasons.some((r) => /M3|Apple silicon|chip/i.test(r));
  const kind: StopKind = macosOld && !chipNamed ? 'macos' : 'chip';
  return { kind, chip: system.chip ?? null, macos: system.macos_version, reasons };
}

export type CheckStatus = 'ok' | 'warn' | 'fail' | 'pending' | 'skip';

export interface CheckInputs {
  system: SystemInfo | null;
  brew: BrewInfo | null;
  engine: EngineDiscoveryInfo | null;
  /** null while loading; 'unavailable' when the manager has no doctor yet (501). */
  doctor: DoctorReport | null | 'unavailable';
}

export function macStatus(system: SystemInfo | null): CheckStatus {
  if (!system) return 'pending';
  return system.supported ? 'ok' : 'fail';
}

export function brewStatus(brew: BrewInfo | null): CheckStatus {
  if (!brew) return 'pending';
  return brew.installed ? 'ok' : 'fail';
}

/** Splash: found and in the supported range (≥ 1.2.0 < 1.3.0); newer minor = warning; older = blocking. */
export function splashStatus(engine: EngineDiscoveryInfo | null): CheckStatus {
  if (!engine) return 'pending';
  if (!engine.found) return 'fail';
  if (engine.support === 'too_old') return 'fail';
  if (engine.support === 'untested') return 'warn';
  return 'ok';
}

export function doctorCheck(doctor: CheckInputs['doctor'], id: string): DoctorReport['checks'][number] | null {
  if (!doctor || doctor === 'unavailable') return null;
  return doctor.checks.find((c) => c.id === id) ?? null;
}

/** The shell-command row: a check the doctor did not report is unknown (skip), never ✓. */
export function shellStatus(doctor: CheckInputs['doctor'], check: DoctorReport['checks'][number] | null): CheckStatus {
  if (!doctor) return 'pending';
  if (doctor === 'unavailable' || !check) return 'skip';
  return check.status;
}

/** Continue on step 1: the Mac is supported and a usable Splash is installed (Homebrew only matters to install it). */
export function engineStepReady(inputs: Pick<CheckInputs, 'system' | 'engine'>): boolean {
  const s = splashStatus(inputs.engine);
  return macStatus(inputs.system) === 'ok' && (s === 'ok' || s === 'warn');
}

// ---------- step 3: preset diff (W5) ----------

/** Every key any preset touches, in first-seen order (the diff shows them all, unchanged ones muted). */
export function presetKeys(presets: readonly PresetOut[]): string[] {
  const keys: string[] = [];
  for (const p of presets) for (const k of Object.keys(p.settings)) if (!keys.includes(k)) keys.push(k);
  return keys;
}

const FALLBACK_FLAGS: Record<string, string | null> = {
  'serve.max_context': '--max-context',
  'serve.max_cache_disk': '--max-cache-disk',
  'serve.persistent_cache': '--persistent-cache',
  'serve.kv_format': '--kv-format',
  'serve.decode_share': '--decode-share',
  'serve.default_reasoning_effort': '--default-reasoning-effort',
  'serve.language_only': '--language-only',
  'routing.auto_load': null,
};

function getPath(obj: unknown, dotted: string): unknown {
  let cur = obj;
  for (const part of dotted.split('.')) {
    if (typeof cur !== 'object' || cur === null) return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return cur;
}

/** Equal as Splash would read them ("0" = "0G" off, "128K" = "131072"). */
export function sameValue(key: string, a: unknown, b: unknown): boolean {
  if (typeof a === 'string' && typeof b === 'string') {
    if (key === 'serve.max_cache_disk') {
      const x = parseMaxCacheDisk(a);
      const y = parseMaxCacheDisk(b);
      if (x.ok && y.ok) return x.bytes === y.bytes;
    }
    if (key === 'serve.max_context') {
      const x = parseMaxContext(a);
      const y = parseMaxContext(b);
      if (x.ok && y.ok) return x.tokens === y.tokens;
    }
  }
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** Display text for a setting value in the diff table. */
export function formatSettingValue(key: string, value: unknown): string {
  if (value === null || value === undefined) {
    return key === 'serve.default_reasoning_effort' ? t('welcome.value.model_default') : '—';
  }
  if (typeof value === 'boolean') return value ? t('welcome.value.on') : t('welcome.value.off');
  if (key === 'serve.max_cache_disk' && typeof value === 'string') {
    const parsed = parseMaxCacheDisk(value);
    if (parsed.ok && parsed.bytes === 0) return t('welcome.value.disk_off');
  }
  return String(value);
}

export interface DiffRow {
  key: string;
  label: string;
  flag: string | null;
  now: unknown;
  nowText: string;
  /** NOW is Splash's default (no stored override). */
  nowIsDefault: boolean;
  next: unknown;
  nextText: string;
  changed: boolean;
  /** Values like 128K depend on RAM: Splash's startup check decides (tooltip). */
  dependsOnRam: boolean;
}

export function presetDiff(
  presets: readonly PresetOut[],
  selected: PresetId,
  effective: EffectiveSettings | null,
  schema: SettingsSchema | null,
  globalSettings?: unknown,
): DiffRow[] {
  const preset = presets.find((p) => p.id === selected);
  return presetKeys(presets).map((key) => {
    const field = schema?.fields.find((f) => f.key === key);
    const ev = effective?.values[key];
    const now = ev ? ev.value : globalSettings !== undefined ? getPath(globalSettings, key) : field?.default;
    const touched = preset !== undefined && Object.prototype.hasOwnProperty.call(preset.settings, key);
    const next = touched ? preset!.settings[key] : now;
    const changed = touched && !sameValue(key, now, next);
    return {
      key,
      label: field?.label ?? t(`welcome.key.${key.replace('.', '_')}` as 'welcome.key.serve_max_context'),
      flag: field ? (field.flag ?? null) : (FALLBACK_FLAGS[key] ?? null),
      now,
      nowText: formatSettingValue(key, now),
      nowIsDefault: ev ? ev.source === 'default' : true,
      next,
      nextText: formatSettingValue(key, next),
      changed,
      dependsOnRam: key === 'serve.max_context' && typeof next === 'string' && next !== 'auto' && changed,
    };
  });
}

/** The preset an earlier wizard run applied (`global.wizard.preset` in settings.json), if any. */
export function savedPreset(globalSettings: unknown): PresetId | null {
  const v = getPath(globalSettings, 'wizard.preset');
  return v === 'coding' || v === 'chat' || v === 'speed' ? v : null;
}

/** The keys and values one PUT would write for this preset (what `apply` does, for tests and the diff). */
export function presetPatch(preset: PresetOut): Record<string, unknown> {
  return { ...preset.settings, 'wizard.preset': preset.id };
}

// ---------- step 4: recommendation rows ----------

export type Fit = 'fits' | 'tight' | 'wont_fit';

export interface ModelRow {
  model: string;
  note: string;
  overrides: Record<string, unknown>;
  primary: boolean;
  languageOnly: boolean;
  format: 'mlx' | 'gguf' | 'legacy' | 'pq2' | null;
  /** The weights (target, or the GGUF variant plus its projector): not what a download fetches. */
  sizeBytes: number | null;
  /** What downloading this pick fetches (as `/inspect`'s `download_plan`); see `downloadBytesOf`. */
  downloadBytes: number | null;
  memoryNeedBytes: number | null;
  fit: Fit | null;
  vision: boolean | null;
  perfNote: string | null;
  installed: boolean;
  /** Won't fit: listed last and not downloadable from the wizard. */
  disabled: boolean;
}

export interface Recommendation {
  preset: PresetOut | null;
  memoryBytes: number;
  reason: string;
  rows: ModelRow[];
}

interface CatalogHit {
  entry: CatalogEntry;
  variant: VariantOut | null;
}

/** Finds a model ID in the catalog: an entry with that exact id, or its repo with a matching GGUF variant. */
export function findInCatalog(catalog: Catalog | null, model: string): CatalogHit | null {
  if (!catalog) return null;
  const { repo, variant } = splitModelId(model);
  let repoHit: CatalogHit | null = null;
  for (const family of catalog.families) {
    for (const group of family.groups) {
      for (const entry of group.entries) {
        if (entry.id === model) return { entry, variant: variant ? (entry.variants?.find((v) => v.name === variant) ?? null) : null };
        if (!repoHit && entry.repo_id === repo) {
          const v = variant ? (entry.variants?.find((x) => x.name === variant) ?? null) : null;
          if (!variant || v) repoHit = { entry, variant: v };
        }
      }
    }
  }
  return repoHit;
}

/**
 * What downloading a pick fetches: the variant's or entry's `download_bytes` (target + vision +
 * draft), or `language_only_download_bytes` when the pick is language-only. Null when unknown, or
 * when a variant was asked for that the catalog does not list.
 */
export function downloadBytesOf(hit: CatalogHit | null, model: string, languageOnly: boolean): number | null {
  if (!hit) return null;
  const sized = hit.variant ?? (splitModelId(model).variant ? null : hit.entry);
  return (languageOnly ? sized?.language_only_download_bytes : sized?.download_bytes) ?? null;
}

function guessFormat(model: string): ModelRow['format'] {
  if (/:PQ2/i.test(model)) return 'pq2';
  if (/gguf/i.test(model)) return 'gguf';
  if (/-Splash$/.test(splitModelId(model).repo)) return 'legacy';
  return 'mlx';
}

export function recommendation(
  list: PresetList | null,
  presetId: PresetId | null,
  catalog: Catalog | null,
  installed: readonly string[] | null,
): Recommendation | null {
  if (!list || list.presets.length === 0) return null;
  const preset = list.presets.find((p) => p.id === presetId) ?? list.presets.find((p) => p.id === 'chat') ?? list.presets[0]!;
  const rec = preset.recommendation;
  const picks = [...(rec.primary ? [{ pick: rec.primary, primary: true }] : []), ...rec.alternatives.map((pick) => ({ pick, primary: false }))];
  const rows: ModelRow[] = picks.map(({ pick, primary }) => {
    const hit = findInCatalog(catalog, pick.model);
    const overrides = (pick.overrides ?? {}) as Record<string, unknown>;
    const fit = (hit?.variant?.fit ?? hit?.entry.fit ?? null) as Fit | null;
    return {
      model: pick.model,
      note: pick.note,
      overrides,
      primary,
      languageOnly: overrides.language_only === true,
      format: hit?.entry.format ?? guessFormat(pick.model),
      sizeBytes: hit?.variant?.size_bytes ?? hit?.entry.size_bytes ?? null,
      downloadBytes: downloadBytesOf(hit, pick.model, overrides.language_only === true),
      memoryNeedBytes: hit?.entry.memory_need_bytes ?? null,
      fit,
      vision: overrides.language_only === true ? false : (hit?.entry.vision ?? null),
      perfNote: hit?.entry.perf_note ?? null,
      installed: (installed?.includes(pick.model) ?? false) || (hit?.entry.installed ?? false),
      disabled: fit === 'wont_fit',
    };
  });
  // Won't-fit rows go last (docs/ui/04 §5); otherwise the API's order (primary first) stays.
  const ordered = [...rows.filter((r) => !r.disabled), ...rows.filter((r) => r.disabled)];
  return { preset, memoryBytes: list.memory_bytes, reason: rec.reason, rows: ordered };
}

/**
 * The catalog tier behind a recommendation (`Recommendation.reason` in manager
 * settings/presets.py: "≥ 48 GB", "36–47 GB", "24–35 GB", or a full sentence) as a sentence.
 */
export function tierSentence(reason: string | null | undefined): string | null {
  const r = (reason ?? '').trim();
  if (!r) return null;
  const min = /^(?:≥|>=)\s*(\d+)\s*GB$/i.exec(r);
  if (min) return t('welcome.model.tier_min', { min: min[1] });
  const range = /^(\d+)\s*[–-]\s*(\d+)\s*GB$/i.exec(r);
  if (range) return t('welcome.model.tier_range', { min: range[1], max: range[2] });
  return /[.!?]$/.test(r) ? r : t('welcome.model.tier_other', { tier: r });
}

/** The download that still owns `model` (queued, running, verifying or paused), if any. */
export function activeDownloadFor<T extends Pick<DownloadItem, 'model' | 'state'>>(items: readonly T[], model: string): T | null {
  return items.find((d) => d.model === model && isActiveDownload(d)) ?? null;
}

/** An installed model as step 4 lists it (docs/ui/04 §5, drift: the Installed group). */
export interface InstalledChoice {
  id: string;
  format: string;
  sizeBytes: number;
  lastUsedAt: string | null;
}

/** Installed and loadable now: not still downloading, paused, verifying or broken. */
const USABLE: ReadonlySet<string> = new Set(['ready', 'active', 'loading', 'update_available']);

/** The engine is serving its model (a failed or stopped engine's `model` is not "active"). */
export function servingModel(e: Pick<EngineSummary, 'state' | 'model'> | null | undefined): string | null {
  return e && isServing(e.state) ? e.model : null;
}

/**
 * Installed models step 4 offers with **Use**, active first, then most recently used, then by
 * ID; only usable installs (ready, active, loading, update available), without the recommendation's
 * own rows (they carry their own Use).
 */
export function installedChoices(
  models: readonly Pick<InstalledModel, 'id' | 'format' | 'size_bytes' | 'last_used_at' | 'status'>[],
  shown: readonly string[],
  activeId: string | null,
): InstalledChoice[] {
  const at = (s: string | null | undefined) => (s ? Date.parse(s) || 0 : 0);
  return models
    .filter((m) => USABLE.has(m.status) && !shown.includes(m.id))
    .sort((a, b) => Number(b.id === activeId) - Number(a.id === activeId) || at(b.last_used_at) - at(a.last_used_at) || a.id.localeCompare(b.id))
    .map((m) => ({ id: m.id, format: m.format, sizeBytes: m.size_bytes, lastUsedAt: m.last_used_at ?? null }));
}

/** The model step 4 preselects when this run has none: the active one, else the most recently used. */
export function defaultInstalled(models: readonly Pick<InstalledModel, 'id' | 'last_used_at' | 'status'>[], activeId: string | null): string | null {
  return installedChoices(models.map((m) => ({ format: 'mlx' as const, size_bytes: 0, ...m })), [], activeId)[0]?.id ?? null;
}

// ---------- step 5: endpoints ----------

export function endpoints(origin: string): { openai: string; anthropic: string } {
  const base = origin.replace(/\/$/, '');
  return { openai: `${base}/v1`, anthropic: base };
}

export function curlSample(origin: string, model: string, withKey: boolean): string {
  const lines = [`curl ${endpoints(origin).openai}/chat/completions \\`, `  -H 'Content-Type: application/json' \\`];
  if (withKey) lines.push(`  -H "Authorization: Bearer $SPLASH_API_KEY" \\`);
  lines.push(`  -d '${JSON.stringify({ model, messages: [{ role: 'user', content: 'Hello' }] })}'`);
  return lines.join('\n');
}

/** The PATH block SPEC §12.1 describes, as one pasteable command (no manager route yet). */
export const PATH_COMMAND = [
  'for f in ~/.zprofile ~/.bash_profile; do',
  "  grep -q '# >>> splash-gui >>>' \"$f\" 2>/dev/null || printf '\\n# >>> splash-gui >>>\\nexport PATH=\"$HOME/.splash/bin:$PATH\"\\n# <<< splash-gui <<<\\n' >> \"$f\"",
  'done',
].join('\n');

/** The origin the manager will answer on after a port change (W2 redirect). */
export function movedOrigin(current: string, port: number): string {
  const url = new URL(current);
  url.port = String(port);
  return url.origin;
}

/** Masked API key for display: `sk-splash-…a91f`. */
export function maskKey(key: string | null | undefined): string {
  if (!key) return '—';
  const dash = key.lastIndexOf('-');
  const prefix = dash > 0 && dash < key.length - 4 ? key.slice(0, dash + 1) : key.slice(0, 3);
  return `${prefix}…${key.slice(-4)}`;
}
