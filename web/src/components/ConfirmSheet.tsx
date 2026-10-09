import type { ComponentChildren } from 'preact';
import { useEffect, useId, useState } from 'preact/hooks';
import { t } from '../strings/en';
import { Button } from './Button';
import { Sheet } from './Sheet';

export interface ConfirmSheetProps {
  open: boolean;
  /** Title ending with a period: "Delete 2 models." */
  title: string;
  /** Consequence list and sizes. */
  children: ComponentChildren;
  /** Confirm button label, carrying the size or count ("Delete · 31.5 GB"). */
  confirmLabel: string;
  /** Shown on the confirm button while `busy`. */
  busyLabel?: string;
  onConfirm: () => void | Promise<void>;
  onClose: () => void;
  /** Typed confirmation word (`DELETE` / `CLEAR`); case-insensitive. */
  typedWord?: string | undefined;
  /** Accent confirm button: only when the action also stops the engine. */
  important?: boolean;
  busy?: boolean;
  /** Disables confirm while sizes are loading. */
  disabled?: boolean;
  error?: string | null;
}

/** Sheet preset for irreversible actions. `role="alertdialog"`. */
export function ConfirmSheet({
  open,
  title,
  children,
  confirmLabel,
  busyLabel,
  onConfirm,
  onClose,
  typedWord,
  important,
  busy,
  disabled,
  error,
}: ConfirmSheetProps) {
  const [typed, setTyped] = useState('');
  const inputId = useId();
  useEffect(() => {
    // Clear on close, not on open: a reset after the sheet opens could wipe what was just typed.
    if (!open) setTyped('');
  }, [open]);
  const typedOk = !typedWord || typed.trim().toUpperCase() === typedWord.toUpperCase();
  const canConfirm = typedOk && !busy && !disabled;
  const confirm = () => {
    if (canConfirm) void onConfirm();
  };
  return (
    <Sheet
      open={open}
      title={title}
      onClose={onClose}
      busy={busy}
      role="alertdialog"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button variant={important ? 'accent' : 'solid'} onClick={confirm} disabled={!canConfirm} loading={busy} data-testid="confirm-sheet-confirm">
            {busy ? (busyLabel ?? confirmLabel) : confirmLabel}
          </Button>
        </>
      }
    >
      <div class="stack confirm-body">
        {children}
        {typedWord && (
          <div class="stack" style={{ gap: '8px' }}>
            <label class="label" for={inputId}>
              {t('confirm.typed_label', { word: typedWord })}
            </label>
            <input
              id={inputId}
              class="input mono"
              value={typed}
              autocomplete="off"
              spellcheck={false}
              onInput={(e) => setTyped(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  confirm();
                }
              }}
            />
          </div>
        )}
        {error && (
          <p class="field-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Sheet>
  );
}
