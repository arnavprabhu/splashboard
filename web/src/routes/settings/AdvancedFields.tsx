/**
 * The fold at the end of a settings section that holds the fields metadata marks `advanced`:
 * rarely needed engine options stay out of the way, and open on their own when one of them is
 * edited, invalid or the target of a `#key` link.
 */
import type { ComponentChildren } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import { t } from '../../strings/settings';

export function AdvancedFields({ keys, reveal, children }: { keys: readonly string[]; reveal: boolean; children: ComponentChildren }) {
  const [open, setOpen] = useState(reveal);
  useEffect(() => {
    if (reveal) setOpen(true);
  }, [reveal]);
  if (keys.length === 0) return null;
  return (
    <details class="disclosure field settings-advanced" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)} data-testid="settings-advanced">
      <summary class="label">
        <span class="disclosure-glyph" aria-hidden="true">
          ▸
        </span>
        {t('settings.advanced_fold', { n: keys.length })}
      </summary>
      <div class="disclosure-body stack">{children}</div>
    </details>
  );
}
