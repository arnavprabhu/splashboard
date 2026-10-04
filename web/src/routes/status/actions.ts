/**
 * Engine actions from the Status page: Stop, Restart, Load/Switch/Retry (docs/ui/02 §4). Buttons
 * stay busy until the engine reports a new state (SSE `engine.state`), or the call fails.
 */

import { useEffect, useState } from 'preact/hooks';
import { ApiError } from '../../api/client';
import { toast, toastError } from '../../components/Toast';
import { engine, setEngine } from '../../store';
import { t } from '../../strings/status';
import { loadModel, restartEngine, stopEngine } from './api';
import { shortName } from './logic';

export type EngineCall = { kind: 'stop' } | { kind: 'restart' } | { kind: 'load'; model: string; switching?: boolean };

export function callKey(call: EngineCall): string {
  return call.kind === 'load' ? `load:${call.model}` : call.kind;
}

export async function runEngineCall(call: EngineCall, force = false): Promise<void> {
  try {
    if (call.kind === 'stop') {
      toast(t('status.toast.stopping'));
      setEngine(await stopEngine());
    } else if (call.kind === 'restart') {
      toast(t('status.toast.restarting'));
      setEngine(await restartEngine());
    } else {
      toast(t(call.switching ? 'status.toast.switching' : 'status.toast.loading', { name: shortName(call.model) }));
      setEngine(await loadModel(call.model, force));
    }
  } catch (err) {
    if (err instanceof ApiError && err.code === 'model_switch_busy') {
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
