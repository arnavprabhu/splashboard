/**
 * The Failure band, loaded only when a start fails so the fix logic stays
 * out of the Status route's first paint.
 */
import { Link } from 'wouter-preact';
import type { EngineError } from '../../api/models';
import type { EngineSummary } from '../../api/types';
import { Button } from '../../components/Button';
import { CodeBlock } from '../../components/CodeBlock';
import { Disclosure } from '../../components/Disclosure';
import { Section } from '../../components/Section';
import { t } from '../../strings/status';
import { FixButtons } from './fixes';
import { viewOf } from './logic';

/** Replaces the numbers after a failed start: headline, raw lines, fixes. */
export default function FailureBand({ engine: e, error, family, onDetails }: { engine: EngineSummary; error: EngineError | null; family: string | null; onDetails: () => void }) {
  const raw = error?.raw ?? viewOf(e.view).log_tail ?? [];
  const budget = error?.kind === 'budget_refusal';
  return (
    <Section label={t('status.failure.label')} id="status-failure">
      <div class="stack">
        <p class="lead">{budget ? t('status.fit.title') : t('status.failure.headline')}</p>
        {error?.message && <p class="body mono">{error.message}</p>}
        {error && (error.suggestions?.length ?? 0) > 0 && (
          <div class="stack" style={{ gap: '8px' }}>
            <h3 class="label">{t('status.failure.fixes')}</h3>
            <FixButtons error={error} model={e.model} family={family} />
          </div>
        )}
        <div class="cluster">
          {budget && (
            <Button variant="text" onClick={onDetails} data-testid="fit-details">
              {t('status.failure.details')}
            </Button>
          )}
          <Link href="/logs?source=engine&level=error" class="btn" data-variant="text">
            {t('status.actions.open_logs')}
          </Link>
        </div>
        {raw.length > 0 && (
          <Disclosure summary={t('status.failure.raw')}>
            <CodeBlock code={raw.join('\n')} what={t('status.failure.raw')} maxHeight={320} />
          </Disclosure>
        )}
      </div>
    </Section>
  );
}
