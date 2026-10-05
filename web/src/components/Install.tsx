/**
 * Lazy entry point for the install progress (./InstallProgress and its strings), so the Status
 * route's initial JS does not carry it (SPEC §18.6 budget): the chunk loads only while Splash
 * installs. The install question loads the same chunk from `withInstallConfirm`.
 */
import { useEffect, useState } from 'preact/hooks';
import type { EngineInstall } from '../api/models';

type Mod = typeof import('./InstallProgress');
let mod: Mod | null = null;
let pending: Promise<Mod> | null = null;

/** Install progress (repo, files, bytes, speed, ETA, the can't-resume warning). */
export function Install({ install }: { install: EngineInstall }) {
  const [m, setM] = useState(mod);
  useEffect(() => {
    if (m) return;
    pending ??= import('./InstallProgress').then((x) => (mod = x));
    pending.then(setM, () => (pending = null));
  }, []);
  return m ? <m.InstallProgress install={install} /> : null;
}
