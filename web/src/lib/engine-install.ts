/**
 * A load that needs a (re)install: `starting.installing` with `EngineView.install` (bytes, speed,
 * ETA from the Hub cache). Splash cannot resume a file (SPEC Q24), so a second Load, Restart or
 * Switch while it installs asks first: the manager answers 409 `install_in_progress` unless the
 * call says `force`. `withInstallConfirm` asks through a ConfirmSheet it mounts on first use.
 */

import { signal } from '@preact/signals';
import { ApiError } from '../api/client';
import type { EngineInstall } from '../api/models';
import type { EngineSummary } from '../api/types';
import { engine } from '../store';

/** The install in progress, or null when the engine is not downloading files. */
export function installOf(e: EngineSummary | null | undefined): EngineInstall | null {
  const v = e?.phase === 'installing' ? e.view.install : null;
  return v && typeof v === 'object' && typeof (v as EngineInstall).repo === 'string' ? (v as EngineInstall) : null;
}

/** The user kept the install going: callers skip their error toast. */
export class Cancelled extends Error {
  constructor() {
    super('cancelled');
    this.name = 'Cancelled';
  }
}

export const isCancelled = (err: unknown): boolean => err instanceof Cancelled;

/** The open question's answer callback (its sheet: InstallConfirm in components/InstallProgress). */
export const installQuestion = signal<((go: boolean) => void) | null>(null);

function ask(): Promise<boolean> {
  installQuestion.value?.(false);
  return new Promise((resolve) => {
    installQuestion.value = (go) => {
      installQuestion.value = null;
      resolve(go);
    };
    // The sheet lives in the lazy install chunk; if it cannot load, keep the install going.
    import('../components/InstallProgress').then(
      (m) => m.mountInstallQuestion(),
      () => installQuestion.value?.(false),
    );
  });
}

export function isInstallConflict(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.code === 'install_in_progress';
}

/** A 409 `install_in_progress` for the model already being installed: nothing to interrupt. */
export function isSameModelInstall(err: unknown): boolean {
  return isInstallConflict(err) && (err as ApiError).details?.same_model === true;
}

/**
 * Runs an engine call that would interrupt an install. While the engine downloads (and for a
 * Load, of another model: loading the model being installed answers 202 and changes nothing), or
 * when the call answers 409 `install_in_progress`, it asks first and runs again with `force`.
 * A 409 with `details.same_model` (an older manager) is that same no-op: it resolves `undefined`
 * without asking, since `force` would not change anything. Throws `Cancelled` on No.
 */
export async function withInstallConfirm<T>(run: (force: boolean) => Promise<T>, model?: string): Promise<T | undefined> {
  const e = engine.value;
  let force = false;
  // Only a real download asks: Splash's startup also passes through `installing` for a model
  // whose files are all present (it verifies them), and then there is nothing to interrupt
  // and the manager answers no 409 (acceptance 2026-10-07).
  if (installOf(e) && (model === undefined || model !== e?.model)) {
    if (!(await ask())) throw new Cancelled();
    force = true;
  }
  try {
    return await run(force);
  } catch (err) {
    if (isSameModelInstall(err)) return undefined;
    if (force || !isInstallConflict(err)) throw err;
    if (!(await ask())) throw new Cancelled();
    return run(true);
  }
}

const ENGINE_CALL = /\/engine\/(load|restart)$/;

/**
 * An alert action that loads or restarts the engine, with `force` added (load: in the body;
 * restart: `?force=true`, as the manager's route takes it). Null for any other action.
 */
export function forcedAction<A extends { path: string; body?: Record<string, unknown> }>(action: A): A | null {
  const [path, query] = action.path.split('?') as [string, string | undefined];
  const kind = ENGINE_CALL.exec(path)?.[1];
  if (!kind) return null;
  return kind === 'load'
    ? { ...action, body: { ...(action.body ?? {}), force: true } }
    : { ...action, path: `${path}?${query ? `${query}&` : ''}force=true` };
}
