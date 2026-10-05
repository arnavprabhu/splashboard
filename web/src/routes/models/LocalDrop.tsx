import { useState } from 'preact/hooks';
import { Button } from '../../components/Button';
import { Section } from '../../components/Section';
import { toast, toastError } from '../../components/Toast';
import { useApi } from '../../lib/use-api';
import { useEvent } from '../../store';
import { systemInfo } from '../../store/live';
import { t } from '../../strings/models';
import { getLocal, localErrors, rescanLocal } from './local';
import { shortName } from './logic';

const MARK = '\u0001';

/**
 * Local GGUF drop-in (SPEC §9.6, D49): where to drop a `.gguf`, a Rescan action, and one plain
 * line per file that was not added with Splash's reason. Refetched on `models.changed` and on
 * the watcher's `local:` alerts; a failed GET leaves the lines out (the alert still shows).
 */
export function LocalDrop({ modelsDir, onRescanned }: { modelsDir: string | null; onRescanned: () => void }) {
  const local = useApi((signal) => getLocal(signal), []);
  const [busy, setBusy] = useState(false);
  useEvent('models.changed', () => void local.reload());
  useEvent('alert', (data) => {
    const subject = (data as { subject?: unknown } | null)?.subject;
    if (typeof subject === 'string' && subject.startsWith('local:')) void local.reload();
  });

  const dir = modelsDir ?? systemInfo.value?.disk.models?.path ?? t('models.local.note_default');
  const [before, after] = t('models.local.note', { dir: MARK }).split(MARK);
  const errors = localErrors(local.data);

  const rescan = async () => {
    setBusy(true);
    try {
      const res = await rescanLocal();
      local.setData({ files: res.files, ignored: res.ignored });
      onRescanned();
      toast(res.added.length ? t('models.local.added', { models: res.added.map(shortName).join(', ') }) : t('models.local.none'));
    } catch (err) {
      toastError(t('models.local.rescan_failed'), err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section label={t('models.local.label')} id="models-local" tight>
      <div class="stack" data-testid="local-drop">
        <div class="cluster local-head">
          <p class="body">
            {before}
            <span class="mono">{dir}</span>
            {after}
          </p>
          <Button variant="text" loading={busy} onClick={() => void rescan()} data-testid="local-rescan">
            {t('models.local.rescan')}
          </Button>
        </div>
        {errors.length > 0 && (
          <ul class="local-errors" aria-label={t('models.local.errors')} data-testid="local-errors">
            {errors.map((e) => (
              <li key={e.file} class="local-error">
                <span class="mono">{e.file}</span>
                <span>{e.error}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}
