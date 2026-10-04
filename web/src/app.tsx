import { useEffect, useState } from 'preact/hooks';
import { Redirect, Route, Router, Switch, useLocation } from 'wouter-preact';
import { ApiError } from './api/client';
import type { Alert, AlertAction } from './api/types';
import { AlertBand } from './components/AlertBand';
import { Banner } from './components/Banner';
import { NavBand, ThemeToggle } from './components/NavBand';
import { Loading } from './components/States';
import { BARE_PATHS, NotFound, ROUTES } from './routes';
import {
  auth,
  dismissAlert,
  engine,
  loadSettings,
  logout,
  managerReachable,
  runAlertAction,
  settingsLoaded,
  sortedAlerts,
  wizardCompleted,
} from './store';

/** Router base without the trailing slash: "/admin". */
export const BASE = import.meta.env.BASE_URL.replace(/\/$/, '');

/** Sends the browser to the login page, remembering where it was (`?next=`). */
export function loginRedirect(): void {
  const login = `${BASE}/login`;
  if (location.pathname === login) return;
  const here = location.pathname.startsWith(BASE) ? location.pathname.slice(BASE.length) || '/' : '/';
  history.pushState(null, '', `${login}?next=${encodeURIComponent(here + location.search)}`);
  dispatchEvent(new PopStateEvent('popstate'));
}

function BareHeader() {
  return (
    <header class="navband">
      <span class="navband-mark">Splash GUI</span>
      <span style={{ flex: '1 1 auto' }} />
      <ThemeToggle />
    </header>
  );
}

/** /admin goes to the welcome wizard until it has been completed (SPEC §10.2), else to Status. */
function RootRedirect() {
  useEffect(() => {
    if (!settingsLoaded.value) void loadSettings();
  }, []);
  if (!settingsLoaded.value) return <Loading />;
  return <Redirect to={wizardCompleted.value === false ? '/welcome' : '/status'} replace />;
}

function useAlertActions() {
  const [, navigate] = useLocation();
  const [actionError, setActionError] = useState<string | null>(null);
  const onAction = (alert: Alert, action: AlertAction) => {
    setActionError(null);
    runAlertAction(action, navigate).catch((err: unknown) => {
      const why = err instanceof ApiError ? err.message : String(err);
      setActionError(`${action.label} failed for "${alert.title ?? alert.message}": ${why}`);
    });
  };
  return { onAction, actionError, clearActionError: () => setActionError(null) };
}

export function Shell() {
  const [location, navigate] = useLocation();
  const bare = BARE_PATHS.includes(location);
  const e = engine.value;
  const a = auth.value;
  const { onAction, actionError, clearActionError } = useAlertActions();
  return (
    <>
      <a href="#main" class="visually-hidden skip-link">
        Skip to content
      </a>
      {bare ? (
        <BareHeader />
      ) : (
        <NavBand
          engineVersion={e?.engine_version ?? null}
          model={e?.model ?? null}
          state={e?.state ?? null}
          authEnabled={a.admin_requires_key && a.authenticated && a.method !== 'cli_token' && a.method !== 'open'}
          onLogout={() => {
            void logout().finally(() => navigate('/login'));
          }}
        />
      )}
      {!bare && <AlertBand alerts={sortedAlerts.value} onAction={onAction} onDismiss={(al) => void dismissAlert(al)} />}
      {!bare && managerReachable.value === false && (
        <section class="band tight" data-testid="offline">
          <Banner tone="warn" title="Not connected">
            Splash GUI's manager is not answering. Start it from the menu bar, or run <code class="mono">splash start</code>.
            This page reconnects by itself.
          </Banner>
        </section>
      )}
      {actionError && (
        <section class="band tight">
          <Banner
            tone="warn"
            title="Action failed"
            actions={
              <button type="button" class="btn" data-variant="text" data-size="s" onClick={clearActionError}>
                Dismiss
              </button>
            }
          >
            {actionError}
          </Banner>
        </section>
      )}
      <main id="main" tabIndex={-1}>
        <Switch>
          <Route path="/">
            <RootRedirect />
          </Route>
          <Route path="/tools">
            <Redirect to="/tools/playground" replace />
          </Route>
          {ROUTES.map((r) => (
            <Route key={r.path} path={r.path} component={r.component} />
          ))}
          <Route component={NotFound} />
        </Switch>
      </main>
    </>
  );
}

export function App({ base = BASE }: { base?: string }) {
  return (
    <Router base={base}>
      <Shell />
    </Router>
  );
}
