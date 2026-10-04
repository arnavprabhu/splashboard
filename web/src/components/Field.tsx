import type { ComponentChildren } from 'preact';
import { useId, useState } from 'preact/hooks';
import { Tag } from './Tag';

export type SettingSource = 'default' | 'global' | 'model';

const SOURCE_LABEL: Record<SettingSource, string> = {
  default: 'Default',
  global: 'Global',
  model: 'This model',
};

export interface FieldProps {
  label: string;
  /** One-line plain-English explanation. */
  help?: ComponentChildren;
  /** The `splash serve` flag this maps to, shown verbatim (flags are case-sensitive). */
  flag?: string;
  source?: SettingSource;
  restart?: boolean;
  /** Deeper explanation behind the "?" disclosure. */
  more?: ComponentChildren;
  error?: string | null;
  /** Restart badge text when `restart` is set ("Restart", "Next load", "Rebind"). */
  restartLabel?: string;
  /** Extra Tags after the source and Restart badges (e.g. a disabled-with-reason Tag). */
  badges?: ComponentChildren;
  /** Advisory lines shown with a `!` prefix; they never block saving (docs/ui/05 §2). */
  warnings?: readonly string[];
  /** Render prop receives the control id and describedby ids. */
  children: (ids: { id: string; describedBy: string }) => ComponentChildren;
}

/** Settings field row (SPEC §10.9): label, help, flag, source chip, Restart badge, "?" disclosure, inline error. */
export function Field({ label, help, flag, source, restart, more, error, restartLabel, badges, warnings, children }: FieldProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const moreId = `${id}-more`;
  const describedBy = [help ? helpId : '', error ? errorId : ''].filter(Boolean).join(' ');
  return (
    <div class="field" data-invalid={error ? 'true' : undefined}>
      <div class="field-head">
        <div class="cluster" style={{ gap: '8px', alignItems: 'center' }}>
          <label class="label" for={id}>
            {label}
          </label>
          {more && (
            <button
              type="button"
              class="qbtn"
              aria-expanded={open}
              aria-controls={moreId}
              aria-label={`More about ${label}`}
              onClick={() => setOpen(!open)}
            >
              ?
            </button>
          )}
        </div>
        {flag && <code class="meta flag">{flag}</code>}
        {(source || restart || badges) && (
          <div class="field-badges">
            {source && <Tag tone={source === 'default' ? 'mute' : 'ink'}>{SOURCE_LABEL[source]}</Tag>}
            {restart && <Tag title="Changing this restarts the engine">{restartLabel ?? 'Restart'}</Tag>}
            {badges}
          </div>
        )}
      </div>
      <div class="field-body">
        {children({ id, describedBy })}
        {help && (
          <p class="field-help mute" id={helpId}>
            {help}
          </p>
        )}
        {error && (
          <p class="field-error" id={errorId} role="alert">
            {error}
          </p>
        )}
        {warnings?.map((w) => (
          <p class="field-help field-warning" key={w}>
            {`! ${w}`}
          </p>
        ))}
        {more && open && (
          <div class="field-more" id={moreId}>
            {more}
          </div>
        )}
      </div>
    </div>
  );
}
