/** `JsonEditor` (docs/ui/07 §11, 08 §1.5): a mono textarea with validation, FORMAT and indentation keys. */
import { Button } from '../../components/Button';
import { t } from '../../strings/tools';
import { checkJson, formatJson, indent, newline } from './json-edit';

export interface JsonEditorProps {
  value: string;
  onChange: (v: string) => void;
  label: string;
  id?: string;
  rows?: number;
  /** Extra validation after parsing (shape checks). */
  validate?: (value: unknown) => string | null;
  /** Shown left of the validity badge (templates, counts). */
  extra?: preact.ComponentChildren;
  onSubmit?: () => void;
}

export function jsonProblem(text: string, validate?: (v: unknown) => string | null): string | null {
  const c = checkJson(text);
  if (!c.ok) return t('tools.json.error', { line: c.line, col: c.col, msg: c.message });
  return validate ? validate(c.value) : null;
}

export function JsonEditor({ value, onChange, label, id, rows = 14, validate, extra, onSubmit }: JsonEditorProps) {
  const problem = value.trim() ? jsonProblem(value, validate) : null;
  return (
    <div class="json-editor stack">
      <div class="cluster json-editor-head">
        <label class="label" for={id}>
          {label}
        </label>
        <span class={problem ? 'label acc' : 'label'} aria-live="polite">
          {problem ? t('tools.json.invalid') : t('tools.json.valid')}
        </span>
        {extra}
        <Button
          size="s"
          variant="text"
          onClick={() => {
            const f = formatJson(value);
            if (f !== null) onChange(f);
          }}
        >
          {t('tools.json.format')}
        </Button>
      </div>
      <textarea
        id={id}
        class="input mono json-editor-area"
        rows={rows}
        spellcheck={false}
        value={value}
        aria-invalid={problem ? 'true' : undefined}
        aria-describedby={problem && id ? `${id}-err` : undefined}
        onInput={(e) => onChange(e.currentTarget.value)}
        onKeyDown={(e) => {
          const el = e.currentTarget;
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            onSubmit?.();
          } else if ((e.key === ']' || e.key === '[') && (e.metaKey || e.ctrlKey)) {
            // ⌘] / ⌘[ indent and outdent; Tab keeps moving focus (no keyboard trap, SPEC §18.4).
            e.preventDefault();
            const r = indent(el.value, el.selectionStart, el.selectionEnd, e.key === '[');
            onChange(r.text);
            requestAnimationFrame(() => el.setSelectionRange(r.selStart, r.selEnd));
          } else if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const r = newline(el.value, el.selectionStart, el.selectionEnd);
            onChange(r.text);
            requestAnimationFrame(() => el.setSelectionRange(r.caret, r.caret));
          }
        }}
      />
      {problem && (
        <p class="field-error" id={id ? `${id}-err` : undefined}>
          {problem}
        </p>
      )}
    </div>
  );
}
