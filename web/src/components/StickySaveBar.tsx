import { Button } from './Button';

export interface StickySaveBarProps {
  changes: number;
  /** True when any pending change needs an engine restart. */
  restart?: boolean;
  saving?: boolean;
  /** Blocks saving while any field is invalid (SPEC D11). */
  invalid?: boolean;
  onSave: () => void;
  onDiscard: () => void;
}

export function saveLabel(restart: boolean | undefined): string {
  return restart ? 'Save & restart engine' : 'Save';
}

export function changesLabel(n: number): string {
  return `${n} ${n === 1 ? 'change' : 'changes'}`;
}

/** Bottom band shown while a form has unsaved changes. */
export function StickySaveBar({ changes, restart, saving, invalid, onSave, onDiscard }: StickySaveBarProps) {
  if (changes <= 0) return null;
  return (
    <div class="savebar" role="region" aria-label="Unsaved changes">
      <span class="label tnum">
        {changesLabel(changes)}
        {invalid && <span class="mute"> · fix errors to save</span>}
      </span>
      <div class="cluster">
        <Button variant="text" onClick={onDiscard} disabled={saving}>
          Discard
        </Button>
        <Button variant={restart ? 'accent' : 'solid'} onClick={onSave} disabled={saving || invalid}>
          {saving ? 'Saving…' : saveLabel(restart)}
        </Button>
      </div>
    </div>
  );
}
