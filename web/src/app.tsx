import { signal } from '@preact/signals';
import type { ComponentChildren } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import { Redirect, Route, Router, Switch, useLocation } from 'wouter-preact';
import { ApiError } from './api/client';
import { backoffDelay } from './api/sse';
import type { Alert, AlertAction } from './api/types';
import { AlertBand } from './components/AlertBand';
import { Banner } from './components/Banner';
import { NavBand, ThemeToggle } from './components/NavBand';
import { OfflineScreen } from './components/OfflineScreen';
import { Sheet } from './components/Sheet';
import { Loading } from './components/States';
import { toast, toastError, ToastHost } from './components/Toast';
import { Kbd } from './components/Tooltip';
import { forcedAction, isCancelled, withInstallConfirm } from './lib/engine-install';
import { formatTokPerSec } from './lib/format';
import { useShortcuts } from './lib/shortcuts';
import { BARE_PATHS, NotFound, ROUTES } from './routes';
import {
  auth,
  authKnown,
  dismissAlert,
  downloadProgress,
  engine,
  eventsAttempt,
  lastSeen,
  loadSettings,
  logout,
  managerReachable,
  reconnectEvents,
  runAlertAction,
  settingsLoaded,
  sortedAlerts,
  toggleTheme,
  wizardCompleted,
} from './store';
import { liveSample, useLiveMetrics } from './store/live';
import { t } from './strings/en';

/** Router base without the trailing slash: "/admin". */
export const BASE = import.meta.env.BASE_URL.replace(/\/$/, '');

/** Pages with unsaved form state set this so the offline state keeps the body (decision S5). */
export const keepBodyWhenOffline = signal(false);

/** Sends the browser to the login page, remembering where it was (`?next=`). */
export function loginRedirect(expired = false): void {
  const login = `${BASE}/login`;
  if (location.pathname === login) return;
  const here = location.pathname.startsWith(BASE) ? location.pathname.slice(BASE.length) || '/' : '/';
  history.pushState(null, '', `${login}?next=${encodeURIComponent(here + location.search)}${expired ? '&expired=1' : ''}`);
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
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const onAction = (alert: Alert, action: AlertAction) => {
    const key = `${alert.id}:${action.id}`;
    setPending((p) => new Set(p).add(key));
    const forced = forcedAction(action);
    const run = (force: boolean) => runAlertAction(force && forced ? forced : action, navigate);
    (forced ? withInstallConfirm(run, typeof action.body?.model === 'string' ? action.body.model : undefined) : run(false))
      .catch((err: unknown) => {
        if (isCancelled(err)) return;
        const why = err instanceof ApiError ? err.message : String(err);
        toastError(t('shell.action_failed', { action: action.label, why }), err);
      })
      .finally(() =>
        setPending((p) => {
          const next = new Set(p);
          next.delete(key);
          return next;
        }),
      );
  };
  return { onAction, pending };
}

/** Countdown to the event stream's next reconnect attempt (docs/ui/01 §5.3). */
function useRetryCountdown(active: boolean): number | null {
  const [left, setLeft] = useState<number | null>(null);
  const attempt = eventsAttempt.value;
  useEffect(() => {
    if (!active) return;
    const due = Date.now() + backoffDelay(Math.max(1, attempt), 1000, 5000);
    const tick = () => {
      const s = Math.ceil((due - Date.now()) / 1000);
      setLeft(s > 0 ? s : null);
    };
    tick();
    const timer = setInterval(tick, 250);
    return () => clearInterval(timer);
  }, [active, attempt]);
  return left;
}

function ShortcutsSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const pages: Array<[string, string]> = [
    ['s', t('nav.status')],
    ['m', t('nav.models')],
    ['c', t('nav.chat')],
    ['t', t('nav.tools')],
    ['i', t('nav.integrations')],
    ['l', t('nav.logs')],
    [',', t('nav.settings')],
  ];
  const rows: Array<[ComponentChildren, string]> = [
    ...pages.map(([k, p]): [ComponentChildren, string] => [
      <>
        <Kbd>g</Kbd> <Kbd>{k}</Kbd>
      </>,
      t('shell.shortcuts.goto', { page: p }),
    ]),
    [
      <>
        <Kbd>g</Kbd> <Kbd>h</Kbd>
      </>,
      t('shell.shortcuts.history'),
    ],
    [
      <>
        <Kbd>g</Kbd> <Kbd>d</Kbd>
      </>,
      t('shell.shortcuts.downloader'),
    ],
    [<Kbd>/</Kbd>, t('shell.shortcuts.search')],
    [<Kbd>?</Kbd>, t('shell.shortcuts.help')],
    [<Kbd>Esc</Kbd>, t('shell.shortcuts.escape')],
    [<Kbd>t</Kbd>, t('shell.shortcuts.theme')],
    [
      <>
        <Kbd>⌘</Kbd> <Kbd>Enter</Kbd>
      </>,
      t('shell.shortcuts.submit'),
    ],
    [<Kbd>.</Kbd>, t('shell.shortcuts.window')],
  ];
  return (
    <Sheet open={open} title={t('shell.shortcuts.title')} onClose={onClose}>
      <dl class="kv">
        {rows.map(([keys, what], i) => (
          <div class="kv-row" key={i}>
            <dt>{keys}</dt>
            <dd class="kv-value">{what}</dd>
          </div>
        ))}
      </dl>
    </Sheet>
  );
}

/** Moves focus to the page's h1 on route change and sets the default title (docs/ui/00 §12.1). */
function useRouteFocus(location: string) {
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const timer = setTimeout(() => {
      const h1 = document.querySelector<HTMLElement>('main h1');
      if (h1) {
        if (!h1.hasAttribute('tabindex')) h1.setAttribute('tabindex', '-1');
        h1.focus({ preventScroll: true });
      }
    }, 60);
    return () => clearTimeout(timer);
  }, [location]);
}

const ENGINE_FREE_PATHS = ['/welcome', '/login', '/settings', '/_design'];

export function Shell() {
  const [location, navigate] = useLocation();
  const bare = BARE_PATHS.includes(location);
  const e = engine.value;
  const a = auth.value;
  const { onAction, pending } = useAlertActions();
  const [help, setHelp] = useState(false);
  const reachable = managerReachable.value;
  const offline = reachable === false;
  const retryIn = useRetryCountdown(offline);
  const wasOffline = useRef(false);
  const busy = e?.state === 'busy';
  useLiveMetrics(undefined, busy && !offline);

  useShortcuts({ navigate, toggleTheme, openHelp: () => setHelp(true) });
  useRouteFocus(location);

  useEffect(() => {
    if (offline) {
      wasOffline.current = true;
      document.title = t('shell.title', { page: t('shell.offline.page_title') });
    } else if (reachable && wasOffline.current) {
      wasOffline.current = false;
      toast(t('shell.connected'));
    }
  }, [offline, reachable]);

  const engineMissing = (e?.view.engine as { found?: boolean } | undefined)?.found === false;
  const loggedOut = authKnown.value && a.admin_requires_key && !a.authenticated;
  const sample = liveSample.value;
  const tokps = busy && sample?.throughput?.decode_tps != null ? formatTokPerSec(sample.throughput.decode_tps) : null;
  const dl = downloadProgress.value;
  const hostedByApp = new URLSearchParams(location.includes('?') ? location.split('?')[1] : globalThis.location?.search ?? '').get('host') === 'app';

  if (engineMissing && !ENGINE_FREE_PATHS.some((p) => location === p || location.startsWith(`${p}/`))) {
    return <Redirect to="/welcome" replace />;
  }

  const showBody = !offline || keepBodyWhenOffline.value || bare;
  return (
    <>
      <a href="#main" class="visually-hidden skip-link">
        {t('shell.skip')}
      </a>
      {bare ? (
        <BareHeader />
      ) : (
        <NavBand
          engineVersion={e?.engine_version ?? null}
          model={e?.model ?? null}
          state={offline ? null : (e?.state ?? null)}
          authEnabled={a.admin_requires_key && a.authenticated && a.method !== 'cli_token' && a.method !== 'open'}
          onLogout={() => {
            void logout().finally(() => navigate('/login'));
          }}
          tokps={tokps}
          modelsProgress={dl === null ? null : `${Math.floor(dl * 100)}%`}
          engineMissing={engineMissing}
          linksHidden={loggedOut}
        />
      )}
      {!bare && !offline && <AlertBand alerts={sortedAlerts.value} onAction={onAction} onDismiss={(al) => void dismissAlert(al)} pending={pending} />}
      {!bare && offline && keepBodyWhenOffline.value && (
        <section class="band tight" data-testid="offline">
          <Banner tone="warn">{t('shell.settings_offline')}</Banner>
        </section>
      )}
      <main id="main" tabIndex={-1}>
        {showBody ? (
          <Switch>
            <Route path="/">
              <RootRedirect />
            </Route>
            <Route path="/tools">
              <Redirect to="/tools/playground" replace />
            </Route>
            <Route path="/settings">
              <Redirect to="/settings/server" replace />
            </Route>
            {ROUTES.map((r) => (
              <Route key={r.path} path={r.path} component={r.component} />
            ))}
            <Route component={NotFound} />
          </Switch>
        ) : (
          <OfflineScreen retryIn={retryIn} onRetry={reconnectEvents} lastSeen={lastSeen.value} hostedByApp={hostedByApp} />
        )}
      </main>
      <ToastHost />
      <ShortcutsSheet open={help} onClose={() => setHelp(false)} />
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

