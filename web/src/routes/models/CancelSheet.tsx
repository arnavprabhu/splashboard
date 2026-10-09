import { useState } from 'preact/hooks';
import type { DownloadItem } from '../../api/models';
import { Button } from '../../components/Button';
import { Sheet } from '../../components/Sheet';
import { formatBytes } from '../../lib/format';
import { t } from '../../strings/models';
import { remove } from './actions';

/**
 * Cancel a download: "Keep files for later" (`keep_files=true`, a
 * later download resumes them) or "Delete partial files" (solid ink). Both remove the row.
 */
export function CancelSheet({ item, onClose }: { item: DownloadItem | null; onClose: () => void }) {
  const [busy, setBusy] = useState<'keep' | 'delete' | null>(null);
  const done = item ? formatBytes(item.bytes_done ?? 0, { base: 1000 }) : '';
  const run = async (keep: boolean) => {
    if (!item) return;
    setBusy(keep ? 'keep' : 'delete');
    await remove(item, keep);
    setBusy(null);
    onClose();
  };
  return (
    <Sheet
      open={item !== null}
      title={t('models.cancel.title')}
      onClose={onClose}
      busy={busy !== null}
      role="alertdialog"
      testId="cancel-sheet"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy !== null}>
            {t('models.cancel.back')}
          </Button>
          <Button variant="outline" onClick={() => void run(true)} loading={busy === 'keep'} disabled={busy !== null} data-testid="cancel-keep">
            {t('models.cancel.keep')}
          </Button>
          <Button variant="solid" onClick={() => void run(false)} loading={busy === 'delete'} disabled={busy !== null} data-testid="cancel-delete">
            {t('models.cancel.delete', { size: done })}
          </Button>
        </>
      }
    >
      {item && (
        <div class="stack">
          <p class="body mono">{item.model}</p>
          <p class="body tnum">{t('models.cancel.so_far', { size: done })}</p>
          <p class="body mute">{t('models.cancel.help')}</p>
        </div>
      )}
    </Sheet>
  );
}
