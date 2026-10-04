/**
 * Settings → Data & privacy (docs/ui/05 §3.15, SPEC D27): what Splash GUI keeps, with sizes,
 * and the manual clear. Every confirmation states the size it deletes.
 */
import { useState } from 'preact/hooks';
import { Link } from 'wouter-preact';
import type { DataSize } from '../../api/models';
import { Button, ConfirmSheet, CopyButton, LoadError, Loading, Table, toast, type Column } from '../../components';
import { DASH, formatBytes, formatCount } from '../../lib/format';
import { useApi } from '../../lib/use-api';
import { t } from '../../strings/settings';
import { settingsApi } from './api';

/** Targets that need a typed CLEAR (decision F4): the persistent cache and every model. */
const TYPED: ReadonlySet<string> = new Set(['kv_cache', 'models']);

export function sizeText(bytes: number | null | undefined): string {
  return bytes === null || bytes === undefined ? DASH : formatBytes(bytes);
}

export function consequence(target: string): string {
  if (target === 'kv_cache') return t('settings.data.consequence.kv_cache');
  if (target === 'responses') return t('settings.data.consequence.responses');
  return t('settings.data.consequence.default');
}

export function DataPrivacy() {
  const sizes = useApi(settingsApi.dataSizes);
  const [target, setTarget] = useState<DataSize | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function clear() {
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      // Splash locks the cache namespace while it runs (docs/ui/05 §9 item 9).
      if (target.target === 'kv_cache') await settingsApi.stopEngine().catch(() => undefined);
      const out = await settingsApi.clearData(target.target);
      toast(t('settings.data.cleared', { what: target.label.toLowerCase(), size: formatBytes(out.freed_bytes) }));
      setTarget(null);
      await sizes.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<DataSize>[] = [
    { key: 'what', label: t('settings.data.what'), render: (r) => r.label },
    { key: 'size', label: t('settings.data.size'), align: 'right', render: (r) => <span class="tnum nowrap">{sizeText(r.bytes)}</span> },
    {
      key: 'note',
      label: t('settings.data.note'),
      render: (r) => (
        <span class="meta data-note">
          {[r.items !== null && r.items !== undefined ? t('settings.data.items', { n: r.items }) : null, r.note]
            .filter(Boolean)
            .join(' · ') || DASH}
        </span>
      ),
    },
    {
      key: 'action',
      label: t('settings.data.actions'),
      hideLabel: true,
      align: 'right',
      render: (r) =>
        r.target === 'models' ? (
          <Link href="/models?select=all" class="btn" data-variant="outline" data-size="s" title={t('settings.data.models_hint')}>
            {t('settings.data.delete')}
          </Link>
        ) : (
          <Button size="s" onClick={() => setTarget(r)} aria-label={`${t('settings.data.clear')} ${r.label}`}>
            {t('settings.data.clear')}
          </Button>
        ),
    },
  ];

  const size = target ? sizeText(target.bytes) : DASH;
  return (
    <div class="stack data-privacy">
      {sizes.error ? (
        <LoadError thing={t('settings.data.thing')} error={sizes.error} onRetry={sizes.reload} />
      ) : !sizes.data ? (
        <Loading />
      ) : (
        <Table columns={columns} rows={sizes.data.targets} rowKey={(r) => r.target} caption={t('settings.desc.data')} />
      )}
      <div class="cluster">
        <p class="meta">{t('settings.data.footer')}</p>
        <CopyButton text={t('settings.data.privacy_summary')} label={t('settings.data.copy_privacy')} />
      </div>
      <ConfirmSheet
        open={!!target}
        title={t('settings.data.clear_title', { what: target?.label ?? '' })}
        confirmLabel={target?.bytes !== null && target?.bytes !== undefined ? t('settings.data.clear_confirm', { size }) : t('common.clear')}
        typedWord={target && TYPED.has(target.target) ? 'CLEAR' : undefined}
        important={target?.target === 'kv_cache' || target?.target === 'responses'}
        busy={busy}
        error={error}
        onClose={() => setTarget(null)}
        onConfirm={clear}
      >
        <p class="body">
          {target?.bytes !== null && target?.bytes !== undefined
            ? t('settings.data.clear_size', { size })
            : t('settings.data.clear_size_unknown')}
          {target?.items !== null && target?.items !== undefined && ` (${formatCount(target.items)})`}
        </p>
        <p class="body">{target ? consequence(target.target) : ''}</p>
        {target?.note && <p class="meta">{target.note}</p>}
      </ConfirmSheet>
    </div>
  );
}
