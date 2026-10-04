import type { EngineState } from '../api/types';

export interface StateDisplay {
  label: string;
  /** Live states get the accent chip with a pulsing dot (SPEC §18.3). */
  live: boolean;
}

/** Chip labels follow SPEC §10.3 (Ready / Generating / Loading / Recovering / Stopped / Failed). */
const DISPLAY: Record<EngineState, StateDisplay> = {
  stopped: { label: 'Stopped', live: false },
  'starting.installing': { label: 'Preparing', live: false },
  'starting.loading': { label: 'Loading', live: false },
  'starting.warming': { label: 'Loading', live: false },
  ready: { label: 'Ready', live: true },
  busy: { label: 'Generating', live: true },
  idle_released: { label: 'Idle', live: false },
  recovering: { label: 'Recovering', live: false },
  engine_failed: { label: 'Failed', live: false },
  stopping: { label: 'Stopping', live: false },
  crashed: { label: 'Restarting', live: false },
  failed: { label: 'Failed', live: false },
};

export function stateDisplay(state: EngineState | null | undefined): StateDisplay {
  return state ? DISPLAY[state] : { label: 'Offline', live: false };
}
