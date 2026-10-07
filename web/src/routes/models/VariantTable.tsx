import { useId } from 'preact/hooks';
import type { VariantOut } from '../../api/models';
import { Button } from '../../components/Button';
import { Table, type Column } from '../../components/Table';
import { Tag } from '../../components/Tag';
import { DASH, formatBytes } from '../../lib/format';
import { t } from '../../strings/models';
import { FitTag } from './bits';
import { isProjector } from './logic';

export interface VariantTableProps {
  variants: readonly VariantOut[];
  /** Radio selection (Download by ID). */
  selected?: string | null;
  onSelect?: (name: string) => void;
  /** Per-row Download buttons (drawer). */
  onDownload?: (variant: VariantOut) => void;
  installed?: ReadonlySet<string>;
  /** Variants whose verdict has not arrived yet (D59): a "Checking…" line, not selectable. */
  pending?: readonly string[];
  caption: string;
}

/** GGUF variants (SPEC §9.1, D8): size, bits, tier, fit, Recommended, disabled rows with reasons. */
export function VariantTable({ variants, selected, onSelect, onDownload, installed, pending, caption }: VariantTableProps) {
  const group = useId();
  const waiting = new Set(pending ?? []);
  const rows = [...variants].sort((a, b) => Number(isProjector(a.name)) - Number(isProjector(b.name)));
  const columns: Column<VariantOut>[] = [
    {
      key: 'name',
      label: t('models.variant.col.variant'),
      render: (v) => {
        const disabled = v.loadable === false || isProjector(v.name) || waiting.has(v.name);
        if (!onSelect || isProjector(v.name)) return <span class="mono">{v.name}</span>;
        return (
          <label class="variant-pick">
            <input
              type="radio"
              name={group}
              value={v.name}
              checked={selected === v.name}
              disabled={disabled}
              onChange={() => onSelect(v.name)}
            />
            <span class="variant-radio" aria-hidden="true" />
            <span class="mono">{v.name}</span>
          </label>
        );
      },
    },
    { key: 'size', label: t('models.variant.col.size'), align: 'right', render: (v) => formatBytes(v.size_bytes, { base: 1000 }) },
    { key: 'bits', label: t('models.variant.col.bits'), align: 'right', render: (v) => (typeof v.bits_per_weight === 'number' ? v.bits_per_weight.toFixed(1) : DASH) },
    { key: 'tier', label: t('models.variant.col.tier'), render: (v) => v.quality ?? DASH },
    { key: 'fit', label: t('models.variant.col.fit'), render: (v) => (v.loadable === false ? DASH : <FitTag fit={v.fit ?? null} />) },
    {
      key: 'status',
      label: t('models.variant.col.status'),
      render: (v) => {
        if (isProjector(v.name)) return <span class="meta">{t('models.variant.projector')}</span>;
        if (v.loadable === false) return <span class="variant-reason">{t('models.variant.unsupported', { reason: v.reason ?? t('models.variant.unsupported_default') })}</span>;
        if (waiting.has(v.name)) return <span class="meta" data-testid="variant-checking">{t('models.variant.checking')}</span>;
        const isInstalled = installed?.has(v.name) ?? false;
        return (
          <span class="cluster" style={{ gap: '8px' }}>
            {v.recommended && <Tag tone="ink">{t('models.variant.recommended')}</Tag>}
            {isInstalled && <span class="meta">{t('models.variant.installed')}</span>}
            {onDownload && !isInstalled && (
              <Button size="s" onClick={() => onDownload(v)} aria-label={t('models.variant.download_label', { name: v.name })}>
                {t('common.download')}
              </Button>
            )}
          </span>
        );
      },
    },
  ];
  return (
    <div class="variant-table" data-testid="variant-table">
      <Table columns={columns} rows={rows} rowKey={(v) => v.name} caption={caption} empty={t('models.variant.none')} />
    </div>
  );
}
