import { useEffect, useId, useState } from 'preact/hooks';
import { ApiError } from '../../api/client';
import type { InstalledModel } from '../../api/models';
import { ConfirmSheet } from '../../components/ConfirmSheet';
import { Checkbox } from '../../components/controls';
import { describeError, toast } from '../../components/Toast';
import { formatBytes } from '../../lib/format';
import { t } from '../../strings/models';
import { deleteModel } from './api';
import { isLocalId } from './local';
import { deletePlan, shortName } from './logic';

export interface DeleteSheetProps {
  open: boolean;
  models: readonly InstalledModel[];
  activeId: string | null;
  /** Free bytes on the models volume now, for "N free now, M after". */
  freeBytes: number | null;
  /** "Delete all models" from Settings → Data (`?select=all`): always typed. */
  all?: boolean;
  onClose: () => void;
  /** Called with the IDs actually deleted. */
  onDone: (deleted: string[]) => void;
}

/**
 * The delete flow: consequence list with ref-counted sizes
 * (`unique_bytes`), shared drafts kept, typed DELETE at ≥ 10 GB or "all", and the active
 * model first with an accent confirm (`DELETE ?confirm_active=true` stops the engine).
 */
export function DeleteSheet({ open, models, activeId, freeBytes, all, onClose, onDone }: DeleteSheetProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The local model's .gguf goes to the Trash only when this is ticked.
  const [trash, setTrash] = useState(false);
  const trashId = useId();
  useEffect(() => {
    if (!open) setTrash(false);
  }, [open]);
  const plan = deletePlan(models, activeId, all);
  const n = models.length;
  const hasLocal = models.some((m) => isLocalId(m.id));
  const title = n === 1 ? t('models.delete.title_one', { short: shortName(models[0]!.id) }) : t('models.delete.title_many', { n });
  const frees = formatBytes(plan.freesBytes);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    const deleted: string[] = [];
    let freed = 0;
    const failures: string[] = [];
    const trashFailed: string[] = [];
    // The active model goes first so the engine is stopped before the rest.
    const ordered = [...models].sort((a, b) => Number(b.id === activeId) - Number(a.id === activeId));
    for (const m of ordered) {
      try {
        const res = await deleteModel(m.id, m.id === activeId, trash && isLocalId(m.id));
        deleted.push(m.id);
        freed += typeof res?.freed_bytes === 'number' ? res.freed_bytes : (m.unique_bytes ?? 0);
        for (const path of res?.trash_failed ?? []) trashFailed.push(path.split('/').pop() ?? path);
      } catch (err) {
        if (err instanceof ApiError && err.code === 'needs_confirmation') {
          // The engine started serving this model after the sheet opened: ask again, accent.
          failures.push(t('models.delete.now_active', { short: shortName(m.id) }));
        } else {
          failures.push(`${m.id}: ${describeError(err)}`);
        }
      }
    }
    setBusy(false);
    if (deleted.length) {
      toast(
        deleted.length === 1
          ? t('models.delete.done_one', { short: shortName(deleted[0]!), size: formatBytes(freed) })
          : t('models.delete.done_many', { n: deleted.length, size: formatBytes(freed) }),
      );
      onDone(deleted);
    }
    for (const name of trashFailed) toast(t('models.delete.trash_failed', { name }), { tone: 'error' });
    if (failures.length) setError(t('models.delete.failed', { why: failures.join(' · ') }));
    else onClose();
  };

  return (
    <ConfirmSheet
      open={open}
      title={title}
      confirmLabel={t('models.delete.confirm', { size: frees })}
      busyLabel={t('common.deleting')}
      onConfirm={confirm}
      onClose={onClose}
      typedWord={plan.typed ? t('confirm.word.delete') : undefined}
      important={plan.includesActive}
      busy={busy}
      disabled={n === 0}
      error={error}
    >
      <div class="stack delete-body" data-testid="delete-sheet">
        {plan.includesActive && (
          <p class="body delete-active" data-testid="delete-active-line">
            {t('models.delete.stops_engine')}
          </p>
        )}
        <p class="body">{t('models.delete.removes')}</p>
        <ul class="delete-list">
          {models.map((m) => (
            <li key={m.id} class="delete-item">
              <span class="mono">{m.id}</span>
              <span class="tnum">{formatBytes(m.size_bytes)}</span>
              {m.unique_bytes !== m.size_bytes && (
                <span class="meta">{t('models.delete.unique', { size: formatBytes(m.unique_bytes) })}</span>
              )}
            </li>
          ))}
          {plan.keptDrafts.map((d) => (
            <li key={d} class="delete-item" data-kept="true">
              <span class="mono">{d}</span>
              <span class="meta">{t('models.delete.draft_kept')}</span>
            </li>
          ))}
        </ul>
        {hasLocal && (
          <div class="stack" data-testid="delete-local">
            <p class="body" data-testid="delete-local-keep">
              {t('models.delete.local_keep')}
            </p>
            <Checkbox id={trashId} checked={trash} onChange={setTrash} label={t('models.delete.trash_label')} />
          </div>
        )}
        <p class="body tnum" data-testid="delete-frees">
          {typeof freeBytes === 'number'
            ? t('models.delete.frees_after', { size: frees, free: formatBytes(freeBytes), after: formatBytes(freeBytes + plan.freesBytes) })
            : t('models.delete.frees', { size: frees })}
        </p>
        {n > 1 && <p class="meta">{t('models.delete.estimate_note')}</p>}
      </div>
    </ConfirmSheet>
  );
}
