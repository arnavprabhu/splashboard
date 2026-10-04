import { useState } from 'preact/hooks';
import { useLocation } from 'wouter-preact';
import type { EngineError, EngineSuggestion } from '../../api/models';
import { Button } from '../../components/Button';
import { toast, toastError } from '../../components/Toast';
import { formatBytes } from '../../lib/format';
import { settings } from '../../store';
import { t } from '../../strings/status';
import { runEngineCall } from './actions';
import { savePatch } from './api';
import { orderSuggestions, shortName, suggestionHref, suggestionKind, suggestionPatch, visionBytes } from './logic';

/** Runs one fix: write the setting and retry/restart, or open the right page (decision T8). */
export async function runSuggestion(s: EngineSuggestion, model: string | null, navigate: (to: string) => void, family?: string | null): Promise<void> {
  const kind = suggestionKind(s);
  if (kind === 'navigate') {
    const href = suggestionHref(s, family);
    if (href) navigate(href);
    return;
  }
  if (kind === 'restart') return runEngineCall({ kind: 'restart' });
  if (kind === 'retry') {
    if (model) await runEngineCall({ kind: 'load', model });
    return;
  }
  const patch = suggestionPatch(s);
  if (patch) {
    try {
      await savePatch(patch, model);
    } catch (err) {
      toastError(t('status.fit.apply_failed'), err);
      throw err;
    }
    if (kind === 'apply_retry' && model) toast(t('status.fit.applied', { name: shortName(model) }));
  }
  if (kind === 'apply_restart') await runEngineCall({ kind: 'restart' });
  else if (model) await runEngineCall({ kind: 'load', model });
}

export interface FixButtonsProps {
  error: EngineError;
  model: string | null;
  family?: string | null | undefined;
  /** Called after a fix starts (e.g. to close the sheet). */
  onDone?: () => void;
  /** Accent on the first fix (the sheet); the failure band leaves the accent to Retry. */
  accentFirst?: boolean;
}

/** The engine's suggested fixes as buttons; the first (lower the context) is the primary one. */
export function FixButtons({ error, model, family, onDone, accentFirst }: FixButtonsProps) {
  const [, navigate] = useLocation();
  const [busy, setBusy] = useState<string | null>(null);
  const maxMemory = (settings.value?.settings.global.serve as Record<string, unknown> | undefined)?.max_memory ?? 'auto';
  const list = orderSuggestions(error.suggestions ?? [], maxMemory);
  if (list.length === 0) return null;
  const vision = visionBytes(error.budget);
  return (
    <div class="stack status-fixes" style={{ gap: '12px' }}>
      {list.map((s, i) => {
        const kind = suggestionKind(s);
        const label =
          kind === 'apply_retry' ? t('status.fit.and_retry', { label: s.label }) : kind === 'apply_restart' ? t('status.fit.and_restart', { label: s.label }) : s.label;
        return (
          <div class="cluster" key={`${s.action}-${i}`} style={{ gap: '6px 16px' }}>
            <Button
              variant={accentFirst && i === 0 ? 'accent' : kind === 'navigate' ? 'text' : 'outline'}
              loading={busy === s.action}
              disabled={busy !== null && busy !== s.action}
              data-testid={`fix-${s.action}`}
              onClick={() => {
                setBusy(s.action);
                runSuggestion(s, model, navigate, family)
                  .then(() => onDone?.())
                  .catch(() => undefined)
                  .finally(() => setBusy(null));
              }}
            >
              {label}
            </Button>
            {s.action === 'language_only' && vision !== null && vision > 0 && <span class="meta">{t('status.fit.vision_note', { bytes: formatBytes(vision) })}</span>}
          </div>
        );
      })}
    </div>
  );
}
