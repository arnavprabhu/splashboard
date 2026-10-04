/**
 * The wizard's step machine and progress persistence (docs/ui/04 §1, decisions W1, W2, W3, W8).
 *
 * Steps are URL state (`?step=1..5`). Progress that must survive a reload or a closed window
 * (the furthest step reached, the port to apply at step 5, the chosen preset and model, the
 * download started in step 4) lives in localStorage until `wizard.step` exists in settings
 * (API request; docs/ui/04 §13).
 */

export const STEPS = [1, 2, 3, 4, 5] as const;
export type Step = (typeof STEPS)[number];
export type PresetId = 'coding' | 'chat' | 'speed';

export const STEP_KEYS: Record<Step, 'engine' | 'storage' | 'usecase' | 'model' | 'start'> = {
  1: 'engine',
  2: 'storage',
  3: 'usecase',
  4: 'model',
  5: 'start',
};

export function isStep(v: unknown): v is Step {
  return typeof v === 'number' && (STEPS as readonly number[]).includes(v);
}

/** `?step=3` → 3; anything else → null. */
export function parseStep(search: string | URLSearchParams): Step | null {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search;
  const raw = params.get('step');
  if (raw === null || !/^\d$/.test(raw.trim())) return null;
  const n = Number(raw);
  return isStep(n) ? n : null;
}

export interface WizardProgress {
  /** The step last shown (resume point when the URL has no `?step`). */
  step: Step;
  /** The furthest step reached; steps up to it can be revisited from the rail. */
  reached: Step;
  /** Port typed in step 2, written at step 5 after Load (W2). null = unchanged. */
  pendingPort: number | null;
  /** Use case chosen in step 3. */
  preset: PresetId | null;
  /** The model picked in step 4 (downloaded or already installed). */
  model: string | null;
  /** The download started in step 4, if any. */
  downloadId: string | null;
}

export const PROGRESS_KEY = 'splash-gui-wizard';

export const EMPTY_PROGRESS: WizardProgress = { step: 1, reached: 1, pendingPort: null, preset: null, model: null, downloadId: null };

const PRESETS: readonly PresetId[] = ['coding', 'chat', 'speed'];

export function isPresetId(v: unknown): v is PresetId {
  return PRESETS.includes(v as PresetId);
}

/** Tolerant reader: unknown or broken fields fall back to the empty progress. */
export function readProgress(raw: unknown): WizardProgress {
  if (typeof raw !== 'object' || raw === null) return { ...EMPTY_PROGRESS };
  const r = raw as Record<string, unknown>;
  const step = isStep(r.step) ? r.step : 1;
  const reached = isStep(r.reached) ? r.reached : step;
  const port = typeof r.pendingPort === 'number' && Number.isInteger(r.pendingPort) && r.pendingPort >= 1 && r.pendingPort <= 65535 ? r.pendingPort : null;
  return {
    step,
    reached: (Math.max(step, reached) as Step),
    pendingPort: port,
    preset: isPresetId(r.preset) ? r.preset : null,
    model: typeof r.model === 'string' && r.model ? r.model : null,
    downloadId: typeof r.downloadId === 'string' && r.downloadId ? r.downloadId : null,
  };
}

export function loadProgress(storage: Pick<Storage, 'getItem'> | undefined = safeStorage()): WizardProgress {
  try {
    const text = storage?.getItem(PROGRESS_KEY);
    return text ? readProgress(JSON.parse(text)) : { ...EMPTY_PROGRESS };
  } catch {
    return { ...EMPTY_PROGRESS };
  }
}

export function saveProgress(progress: WizardProgress, storage: Pick<Storage, 'setItem'> | undefined = safeStorage()): void {
  try {
    storage?.setItem(PROGRESS_KEY, JSON.stringify(progress));
  } catch {
    /* storage blocked: the URL still carries the step */
  }
}

export function clearProgress(storage: Pick<Storage, 'removeItem'> | undefined = safeStorage()): void {
  try {
    storage?.removeItem(PROGRESS_KEY);
  } catch {
    /* ignore */
  }
}

function safeStorage(): Storage | undefined {
  try {
    return globalThis.localStorage;
  } catch {
    return undefined;
  }
}

/** The step to show: the URL wins (deep links, reload), else the stored resume point. */
export function initialStep(urlStep: Step | null, progress: WizardProgress): Step {
  return urlStep ?? progress.step;
}

/** Moving to `step` records it as shown and extends `reached`. */
export function visit(progress: WizardProgress, step: Step): WizardProgress {
  return { ...progress, step, reached: (Math.max(progress.reached, step) as Step) };
}

export type RailState = 'done' | 'current' | 'future';

/**
 * Rail item state: the current step; steps before it or already reached are done (and can be
 * revisited); later steps that were never reached are future (not clickable).
 */
export function railState(item: Step, current: Step, reached: Step): RailState {
  if (item === current) return 'current';
  if (item < current || item <= reached) return 'done';
  return 'future';
}

export function nextStep(step: Step): Step | null {
  return step < 5 ? ((step + 1) as Step) : null;
}

export function prevStep(step: Step): Step | null {
  return step > 1 ? ((step - 1) as Step) : null;
}

/** W1: Skip setup is offered from step 3 on. */
export function canSkip(step: Step): boolean {
  return step >= 3;
}

/** Search string for a step, keeping the other query parameters (`host`, `theme`). */
export function stepSearch(search: string, step: Step): string {
  const params = new URLSearchParams(search);
  params.set('step', String(step));
  return params.toString();
}
