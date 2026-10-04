import type { InspectResult } from '../../api/models';
import { Tag } from '../../components/Tag';
import { formatBytes } from '../../lib/format';
import { t } from '../../strings/models';

export type Fit = 'fits' | 'tight' | 'wont_fit' | null;

/** Fits · Tight · Won't fit (00 §5.3). Won't fit is mute, never accent. */
export function FitTag({ fit, needBytes, memoryBytes }: { fit: Fit; needBytes?: number | null; memoryBytes?: number | null }) {
  if (!fit) return null;
  if (fit === 'fits') return <Tag tone="ink">{t('models.fit.fits')}</Tag>;
  if (fit === 'tight') {
    const title = memoryBytes ? t('models.fit.tight_help', { ram: formatBytes(memoryBytes) }) : undefined;
    return (
      <Tag tone="ink" title={title}>
        {t('models.fit.tight')}
      </Tag>
    );
  }
  const title = needBytes && memoryBytes ? t('models.fit.wont_help', { need: formatBytes(needBytes), ram: formatBytes(memoryBytes) }) : undefined;
  return (
    <Tag tone="mute" title={title}>
      {t('models.fit.wont_fit')}
    </Tag>
  );
}

export type CompatState = 'unchecked' | 'checking' | 'error' | InspectResult['badge'];

/** Compatible (acc) · Compatible — text only (ink) · Incompatible (mute) · Checking… · Not checked (00 §5.3). */
export function CompatTag({ state, acc = true }: { state: CompatState; acc?: boolean }) {
  switch (state) {
    case 'compatible':
    case 'not_clef_accurate':
      return <Tag tone={acc ? 'acc' : 'ink'}>{t('models.compat.compatible')}</Tag>;
    case 'text_only':
      return <Tag tone="ink">{t('models.compat.text_only')}</Tag>;
    case 'incompatible':
      return <Tag tone="mute">{t('models.compat.incompatible')}</Tag>;
    case 'checking':
      return (
        <Tag tone="mute" dots>
          {t('models.compat.checking')}
        </Tag>
      );
    case 'error':
      return <Tag tone="mute">{t('models.compat.error')}</Tag>;
    default:
      return <Tag tone="mute">{t('models.compat.unchecked')}</Tag>;
  }
}
