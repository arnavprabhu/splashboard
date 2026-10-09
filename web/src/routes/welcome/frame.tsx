/**
 * Shared wizard frame: context, the StepRail (docs/ui/04 §1), the step layout (rail in the
 * label column, step in the content column) and the footer band with Back / Continue / Skip.
 */

import { signal } from '@preact/signals';
import type { ComponentChildren } from 'preact';
import { createContext } from 'preact';
import { useContext, useState } from 'preact/hooks';
import { useLocation } from 'wouter-preact';
import type { SystemInfo } from '../../api/models';
import { Button, toast, toastError } from '../../components';
import { formatBytes } from '../../lib/format';
import { t } from '../../strings/welcome';
import { markCompleted, saveWizardProgress } from './api';
import { closeWelcome } from './host';
import { settings } from '../../store';
import { canSkip, clearProgress, loadProgress, mergeServerProgress, prevStep, railState, saveProgress, STEPS, STEP_KEYS, toServerProgress, type Step, type WizardProgress, type WizardServerProgress } from './steps';

// ---------- progress (persisted, W8) ----------

export const progress = signal<WizardProgress>(loadProgress());

/** Local copy first (it also holds `reached` and the download id), then settings (F3). */
export function updateProgress(patch: Partial<WizardProgress>): void {
  progress.value = { ...progress.value, ...patch };
  saveProgress(progress.value);
  // A failed write keeps the local copy, the fallback when settings cannot be saved.
  saveWizardProgress(toServerProgress(progress.value)).catch(() => undefined);
}

/** Takes the progress settings hold (another browser may have written it) over the local copy. */
export function hydrateProgress(): void {
  const wizard = settings.value?.settings.global?.wizard as Partial<WizardServerProgress> | undefined;
  progress.value = mergeServerProgress(progress.value, wizard);
}

export function resetProgress(): void {
  clearProgress();
  progress.value = loadProgress();
}

// ---------- context ----------

export interface WizardCtx {
  step: Step;
  goTo: (step: Step) => void;
  hosted: boolean;
  system: SystemInfo | null;
}

export const WizardContext = createContext<WizardCtx>({ step: 1, goTo: () => undefined, hosted: false, system: null });

export const useWizard = () => useContext(WizardContext);

export function stepName(step: Step): string {
  return t(`welcome.step.${STEP_KEYS[step]}` as 'welcome.step.engine');
}

/** "Apple M5 Pro · 64 GB · macOS 27.0 · 318 GB free" under the rail. */
export function macMeta(system: SystemInfo | null): string | null {
  if (!system) return null;
  return t('welcome.mac_meta', {
    chip: system.chip ?? t('welcome.unknown_chip'),
    memory: formatBytes(system.memory_bytes, { digits: 0 }),
    macos: system.macos_version,
    free: formatBytes(system.disk?.models?.free_bytes ?? null, { digits: 0 }),
  });
}

// ---------- rail ----------

export function StepRail() {
  const { step, goTo, system } = useWizard();
  const reached = progress.value.reached;
  const meta = macMeta(system);
  return (
    <nav class="wz-rail" aria-label={t('welcome.rail.label')}>
      <ol class="wz-rail-list">
        {STEPS.map((s) => {
          const state = railState(s, step, reached);
          const label = (
            <>
              <span class="wz-rail-index tnum">{String(s).padStart(2, '0')}</span>
              <span class="wz-rail-name">{stepName(s)}</span>
              <span class="wz-rail-glyph" aria-hidden="true">
                {state === 'done' ? '✓' : state === 'current' ? '●' : ''}
              </span>
              {state === 'done' && <span class="visually-hidden"> ({t('welcome.rail.done')})</span>}
            </>
          );
          return (
            <li key={s} class="wz-rail-item" data-state={state}>
              {state === 'future' ? (
                <span class="wz-rail-row">{label}</span>
              ) : (
                <button
                  type="button"
                  class="wz-rail-row"
                  aria-current={state === 'current' ? 'step' : undefined}
                  onClick={() => state !== 'current' && goTo(s)}
                >
                  {label}
                </button>
              )}
            </li>
          );
        })}
      </ol>
      {meta && <p class="meta wz-rail-meta">{meta}</p>}
    </nav>
  );
}

// ---------- layout ----------

export interface FooterProps {
  onContinue?: (() => void) | undefined;
  continueLabel?: string;
  continueDisabled?: boolean;
  /** The step's blocking action holds the accent; Continue is then outlined. */
  continueVariant?: 'accent' | 'outline' | 'solid';
  busy?: boolean;
  hideBack?: boolean;
  hideContinue?: boolean;
  /** Meta line left of the buttons ("Port applies when the server starts."). */
  note?: ComponentChildren;
}

export interface StepLayoutProps {
  lead?: ComponentChildren;
  children: ComponentChildren;
  footer?: FooterProps | null;
}

export function StepLayout({ lead, children, footer }: StepLayoutProps) {
  const { step } = useWizard();
  return (
    <>
      <section class="band wz-frame" aria-labelledby="wz-title">
        <div class="label-row wz-row">
          <div class="label-row-label wz-railcol">
            <StepRail />
          </div>
          <div class="label-row-content wz-content stack">
            <p class="meta wz-stepmeta" data-testid="step-meta">
              {t('welcome.step_meta', { n: step, name: stepName(step) })}
            </p>
            {lead && <p class="lead wz-lead">{lead}</p>}
            {children}
          </div>
        </div>
      </section>
      {footer !== null && <StepFooter {...(footer ?? {})} />}
    </>
  );
}

export function StepFooter({ onContinue, continueLabel, continueDisabled, continueVariant = 'accent', busy, hideBack, hideContinue, note }: FooterProps) {
  const { step, goTo, hosted } = useWizard();
  const [, navigate] = useLocation();
  const [skipping, setSkipping] = useState(false);
  const back = prevStep(step);
  const skip = async () => {
    setSkipping(true);
    try {
      await markCompleted();
      resetProgress();
      toast(t('welcome.skip.toast'));
      if (hosted) await closeWelcome(true).catch(() => undefined);
      navigate('/status');
    } catch (err) {
      toastError(t('welcome.save_failed'), err);
    } finally {
      setSkipping(false);
    }
  };
  return (
    <section class="band tight wz-foot" aria-label={t('welcome.foot.label')}>
      <div class="wz-foot-row">
        <div class="wz-foot-left cluster">
          {canSkip(step) && (
            <Button variant="text" onClick={() => void skip()} loading={skipping} data-testid="skip-setup">
              {t('welcome.nav.skip')}
            </Button>
          )}
          {note && <p class="meta">{note}</p>}
        </div>
        <div class="wz-foot-actions">
          {!hideContinue && onContinue && (
            <Button
              variant={continueVariant}
              onClick={onContinue}
              disabled={continueDisabled}
              loading={busy}
              data-testid="continue"
            >
              {continueLabel ?? t('welcome.nav.continue')}
            </Button>
          )}
          {!hideBack && back !== null && (
            <Button onClick={() => goTo(back)} data-testid="back">
              {t('welcome.nav.back')}
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}

/** One check row in step 1 / status lines: label · value · glyph (✓ ink, ! ink, ✕ accent). */
export function Glyph({ status }: { status: 'ok' | 'warn' | 'fail' | 'pending' | 'skip' }) {
  if (status === 'pending') return <span class="wz-glyph meta loading-dots" aria-label={t('welcome.glyph.pending')} />;
  const text = status === 'ok' ? '✓' : status === 'warn' ? '!' : status === 'fail' ? '✕' : '–';
  return (
    <span class="wz-glyph" data-status={status} role="img" aria-label={t(`welcome.glyph.${status}` as 'welcome.glyph.ok')}>
      {text}
    </span>
  );
}
