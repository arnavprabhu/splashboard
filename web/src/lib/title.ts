import { useEffect } from 'preact/hooks';
import { t } from '../strings/en';

/**
 * Sets `document.title`. Pass the page part only: `useTitle('Models')` gives
 * "Models — Splashboard"; pass `raw: true` for a fully formed title.
 */
export function useTitle(page: string | null | undefined, raw = false): void {
  useEffect(() => {
    if (!page) return;
    document.title = raw ? page : t('shell.title', { page });
  }, [page, raw]);
}
