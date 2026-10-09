import type { ComponentType, FunctionComponent } from 'preact';
import { useEffect, useState } from 'preact/hooks';
import { ErrorState, Loading } from '../components/States';

type Loader<P> = () => Promise<{ default: ComponentType<P> }>;

export type LazyComponent<P> = FunctionComponent<P> & { preload: () => Promise<unknown> };

/** Code-split component without preact/compat: loads once, shows Loading, then renders. */
export function lazyRoute<P extends object>(load: Loader<P>): LazyComponent<P> {
  let cached: ComponentType<P> | null = null;
  let pending: Promise<ComponentType<P>> | null = null;

  const fetchOnce = () => {
    pending ??= load().then(
      (m) => (cached = m.default),
      (err: unknown) => {
        pending = null;
        throw err;
      },
    );
    return pending;
  };

  const Lazy = ((props: P) => {
    const [Comp, setComp] = useState<ComponentType<P> | null>(() => cached);
    const [error, setError] = useState<unknown>(null);
    const [attempt, setAttempt] = useState(0);
    useEffect(() => {
      if (Comp) return;
      let live = true;
      fetchOnce().then(
        (c) => live && setComp(() => c),
        (err: unknown) => live && setError(err),
      );
      return () => {
        live = false;
      };
    }, [attempt]);
    if (error)
      return (
        <section class="band">
          <ErrorState
            error={new Error('This page failed to load. Splashboard may have been updated; reload the page.')}
            onRetry={() => {
              setError(null);
              setAttempt(attempt + 1);
            }}
          />
        </section>
      );
    if (!Comp)
      return (
        <section class="band">
          <Loading />
        </section>
      );
    return <Comp {...props} />;
  }) as LazyComponent<P>;
  Lazy.preload = fetchOnce;
  return Lazy;
}
