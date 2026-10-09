/**
 * Engine actions from the Status page: Stop, Restart, Load/Switch/Retry. Buttons
 * stay busy until the engine reports a new state (SSE `engine.state`), or the call fails.
 */

import { useEffect, useState } from 'preact/hooks';
import { ApiError } from '../../api/client';
import { toast, toastError } from '../../components/Toast';
import { isCancelled, withInstallConfirm } from '../../lib/engine-install';
import { engine, setEngine } from '../../store';
import { t } from '../../strings/status';
import { loadModel, restartEngine, stopEngine } from './api';
import { shortName } from './logic';

export type EngineCall = { kind: 'stop' } | { kind: 'restart' } | { kind: 'load'; model: string; switching?: boolean };

export function callKey(call: EngineCall): string {
  return call.kind === 'load' ? `load:${call.model}` : call.kind;
}

export async function runEngineCall(call: EngineCall, force = false): Promise<void> {
  // One toast per call, even when a 409 install_in_progress makes withInstallConfirm run it twice.
  let toasted = false;
  const once = (text: string) => {
    if (!toasted) toast(text);
    toasted = true;
  };
  try {
    if (call.kind === 'stop') {
      toast(t('status.toast.stopping'));
      setEngine(await stopEngine());
    } else if (call.kind === 'restart') {
      setEngine(
        await withInstallConfirm((f) => {
          once(t('status.toast.restarting'));
          return restartEngine(force || f);
        }),
      );
    } else {
      setEngine(
        await withInstallConfirm((f) => {
          once(t(call.switching ? 'status.toast.switching' : 'status.toast.loading', { name: shortName(call.model) }));
          return loadModel(call.model, force || f);
        }, call.model),
      );
    }
  } catch (err) {
    if (isCancelled(err)) {
      /* the install keeps going */
    } else if (err instanceof ApiError && err.code === 'model_switch_busy') {
      toast(t('status.toast.switch_busy'));
    } else {
      const headline = call.kind === 'stop' ? 'status.toast.failed_stop' : call.kind === 'restart' ? 'status.toast.failed_restart' : 'status.toast.failed_load';
      toastError(t(headline), err);
    }
    throw err;
  }
}

/** Tracks which call is pending; clears it when the engine state or model changes. */
export function useEngineCalls(): { pending: string | null; run: (call: EngineCall, force?: boolean) => void } {
  const [pending, setPending] = useState<string | null>(null);
  const e = engine.value;
  const stamp = `${e?.state ?? ''}|${e?.model ?? ''}`;
  useEffect(() => {
    setPending(null);
  }, [stamp]);
  const run = (call: EngineCall, force = false) => {
    setPending(callKey(call));
    runEngineCall(call, force).catch(() => setPending(null));
  };
  return { pending, run };
}
