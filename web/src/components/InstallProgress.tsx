import { render } from 'preact';
import type { EngineInstall } from '../api/models';
import { installOf, installQuestion } from '../lib/engine-install';
import { formatBytes, formatBytesPerSecond, formatDuration } from '../lib/format';
import { engine } from '../store';
import { t } from '../strings/install';
import { ConfirmSheet } from './ConfirmSheet';
import { ProgressBar } from './ProgressBar';

/** Hub sizes are decimal, as in the downloads panel. */
const HUB = { base: 1000 } as const;

function facts(i: EngineInstall): string {
  return [
    t('install.of', { done: formatBytes(i.done_bytes, HUB), total: formatBytes(i.total_bytes, HUB) }),
    i.speed_bps ? formatBytesPerSecond(i.speed_bps, HUB) : null,
    typeof i.eta_s === 'number' ? t('install.left', { eta: formatDuration(i.eta_s) }) : null,
  ]
    .filter(Boolean)
    .join(' · ');
}

/** What Splash is downloading before it loads (`starting.installing`): repo, files, bytes, speed, ETA. */
export function InstallProgress({ install: i }: { install: EngineInstall }) {
  const text = facts(i);
  return (
    <div class="stack install-progress" data-testid="engine-install">
      <p class="meta">
        <span class="mono">
          {i.repo}@{i.revision.slice(0, 7)}
        </span>{' '}
        · {t('install.files', { n: i.files })}
      </p>
      <ProgressBar live value={i.total_bytes > 0 ? i.done_bytes / i.total_bytes : null} label={t('install.label')} valueText={text} />
      <p class="meta tnum">{text}</p>
      <p class="meta">{t('install.warn')}</p>
    </div>
  );
}

/** The shell's answer to `withInstallConfirm`: interrupt the install, or keep it going. */
export function InstallConfirm({ install: i, model, answer }: { install: EngineInstall | null; model: string | null; answer: (go: boolean) => void }) {
  return (
    <ConfirmSheet open title={t('install.confirm.title')} confirmLabel={t('install.confirm.go')} important onConfirm={() => answer(true)} onClose={() => answer(false)}>
      <p class="body">{t('install.confirm.body', { repo: i?.repo ?? model ?? '—' })}</p>
      {i && <p class="meta tnum">{facts(i)}</p>}
    </ConfirmSheet>
  );
}

function Question() {
  const answer = installQuestion.value;
  return answer ? <InstallConfirm install={installOf(engine.value)} model={engine.value?.model ?? null} answer={answer} /> : null;
}

let root: HTMLElement | null = null;

/** Mounts the question's sheet once, in its own root (the shell does not carry this chunk). */
export function mountInstallQuestion(): void {
  if (root) return;
  root = document.createElement('div');
  document.body.append(root);
  render(<Question />, root);
}
