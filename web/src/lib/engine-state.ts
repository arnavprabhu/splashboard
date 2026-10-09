import type { EngineState } from '../api/types';
import { t } from '../strings/en';

export interface StateDisplay {
  label: string;
  /** Live states get the accent chip with a pulsing dot. */
  live: boolean;
  /** In-progress states show animated dots after the label. */
  busy?: boolean;
}

/** The full chip vocabulary. Only Ready and Generating are live. */
const DISPLAY: Record<EngineState, StateDisplay> = {
  stopped: { label: t('state.stopped'), live: false },
  'starting.installing': { label: t('state.preparing'), live: false, busy: true },
  'starting.loading': { label: t('state.loading'), live: false, busy: true },
  'starting.warming': { label: t('state.loading'), live: false, busy: true },
  ready: { label: t('state.ready'), live: true },
  busy: { label: t('state.generating'), live: true },
  idle_released: { label: t('state.idle'), live: false },
  recovering: { label: t('state.recovering'), live: false },
  engine_failed: { label: t('state.failed'), live: false },
  stopping: { label: t('state.stopping'), live: false, busy: true },
  crashed: { label: t('state.restarting'), live: false, busy: true },
  failed: { label: t('state.failed'), live: false },
};

export function stateDisplay(state: EngineState | null | undefined): StateDisplay {
  return state ? DISPLAY[state] : { label: t('state.offline'), live: false };
}

/** States in which the engine process exists (Stop makes sense). */
export function isRunning(state: EngineState | null | undefined): boolean {
  return !!state && state !== 'stopped' && state !== 'failed' && state !== 'stopping';
}

/** States in which requests are answered right away. */
export function isServing(state: EngineState | null | undefined): boolean {
  return state === 'ready' || state === 'busy' || state === 'idle_released';
}
