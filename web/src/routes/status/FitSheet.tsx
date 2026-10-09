import type { EngineError } from '../../api/models';
import { CodeBlock } from '../../components/CodeBlock';
import { Disclosure } from '../../components/Disclosure';
import { KeyValue } from '../../components/KeyValue';
import { Sheet } from '../../components/Sheet';
import { formatBytes, formatCount } from '../../lib/format';
import { t } from '../../strings/status';
import { FixButtons } from './fixes';
import { parseContextRefusal } from './logic';

export interface FitSheetProps {
  open: boolean;
  onClose: () => void;
  error: EngineError;
  model: string | null;
  family: string | null;
}

/** "Model does not fit.": breakdown, raw output, one-click fixes. */
export default function FitSheet({ open, onClose, error, model, family }: FitSheetProps) {
  const refusal = parseContextRefusal(error);
  const rows = error.budget ?? [];
  return (
    <Sheet open={open} title={t('status.fit.title')} onClose={onClose} testId="fit-sheet">
      <div class="stack" style={{ gap: '28px' }}>
        {refusal && model ? (
          <p class="lead">{t('status.fit.lead', { allowed: formatCount(refusal.allowed), model, asked: formatCount(refusal.asked) })}</p>
        ) : (
          <div class="stack" style={{ gap: '8px' }}>
            <p class="lead">{t('status.failure.headline')}</p>
            <p class="body mono">{error.message}</p>
          </div>
        )}
        <div class="stack" style={{ gap: '8px' }}>
          <h3 class="label">{t('status.fit.breakdown')}</h3>
          {rows.length > 0 ? (
            <KeyValue
              label={t('status.fit.breakdown')}
              items={rows.map((r, i) => ({
                key: `${i}-${r.label}`,
                label: <span class="mono status-fit-key">{r.label}</span>,
                value: r.bytes != null ? formatBytes(r.bytes) : r.text,
                meta: r.bytes != null && r.text && r.text !== formatBytes(r.bytes) ? <span class="mono">{r.text}</span> : undefined,
              }))}
            />
          ) : (
            <p class="body mute">{t('status.fit.no_budget')}</p>
          )}
        </div>
        {(error.raw?.length ?? 0) > 0 && (
          <Disclosure summary={t('status.failure.raw')}>
            <CodeBlock code={(error.raw ?? []).join('\n')} what={t('status.failure.raw')} maxHeight={320} />
          </Disclosure>
        )}
        <div class="stack" style={{ gap: '12px' }}>
          <h3 class="label">{t('status.failure.fixes')}</h3>
          <FixButtons error={error} model={model} family={family} onDone={onClose} accentFirst />
        </div>
      </div>
    </Sheet>
  );
}
