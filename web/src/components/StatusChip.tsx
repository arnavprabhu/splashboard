import type { EngineState } from '../api/types';
import { stateDisplay } from '../lib/engine-state';

export interface StatusChipProps {
  state?: EngineState | null;
  /** Overrides the label derived from `state`. */
  label?: string;
  /** Overrides liveness derived from `state`. */
  live?: boolean;
}

/** Accent fill and a pulsing dot for live states only; an ink outline otherwise. */
export function StatusChip({ state, label, live }: StatusChipProps) {
  const display = stateDisplay(state);
  const isLive = live ?? display.live;
  return (
    <span class="chip" data-live={String(isLive)} data-state={state ?? 'offline'} role="status">
      <span class="chip-dot" aria-hidden="true">
        ●
      </span>
      {label ?? display.label}
    </span>
  );
}
