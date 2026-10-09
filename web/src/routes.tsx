import type { ComponentType } from 'preact';
import { lazyRoute } from './lib/lazy';

export interface RouteDef {
  path: string;
  // Pages take `{ params }` from wouter; their own prop types are narrower than RouteComponentProps.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: ComponentType<any>;
  /** Pages that render without the main nav band (full-page flows). */
  bare?: boolean;
}

// Every page is its own chunk.
const Status = lazyRoute(() => import('./routes/status'));
const StatusHistory = lazyRoute(() => import('./routes/status-history'));
const Models = lazyRoute(() => import('./routes/models'));
const Downloader = lazyRoute(() => import('./routes/models-downloader'));
const ModelSettings = lazyRoute(() => import('./routes/model-settings'));
const Chat = lazyRoute(() => import('./routes/chat'));
const Playground = lazyRoute(() => import('./routes/tools-playground'));
const Tokenizer = lazyRoute(() => import('./routes/tools-tokenizer'));
const Judgments = lazyRoute(() => import('./routes/tools-judgments'));
const Benchmark = lazyRoute(() => import('./routes/tools-benchmark'));
const Integrations = lazyRoute(() => import('./routes/integrations'));
const Logs = lazyRoute(() => import('./routes/logs'));
const Diagnostics = lazyRoute(() => import('./routes/logs-diagnostics'));
const Settings = lazyRoute(() => import('./routes/settings'));
const Welcome = lazyRoute(() => import('./routes/welcome'));
const Login = lazyRoute(() => import('./routes/login'));
const Design = lazyRoute(() => import('./routes/design'));
export const NotFound = lazyRoute(() => import('./routes/not-found'));
/**
 * Parts of the Status page below the fold. Declared here, not in the route: a dynamic import
 * inside the Status chunk makes Rolldown wrap that chunk in a namespace helper that drags the
 * Chat route into the initial set.
 */
export const StatusLowerBands = lazyRoute(() => import('./routes/status/Bands'));
export const StatusFitSheet = lazyRoute(() => import('./routes/status/FitSheet'));
export const StatusFailureBand = lazyRoute(() => import('./routes/status/FailureBand'));

/** Paths are relative to the router base (/admin). Order matters: first match wins. */
export const ROUTES: readonly RouteDef[] = [
  { path: '/status', component: Status },
  { path: '/status/history', component: StatusHistory },
  { path: '/models', component: Models },
  { path: '/models/downloader', component: Downloader },
  { path: '/models/:owner/:repo/settings', component: ModelSettings },
  { path: '/models/:id/settings', component: ModelSettings },
  // One entry with an optional param: /chat → /chat/:cid after the first send must not remount
  // the page (it would drop the reply that is streaming).
  { path: '/chat/:cid?', component: Chat },
  { path: '/tools/playground', component: Playground },
  { path: '/tools/tokenizer', component: Tokenizer },
  { path: '/tools/judgments', component: Judgments },
  { path: '/tools/benchmark', component: Benchmark },
  { path: '/integrations', component: Integrations },
  { path: '/logs', component: Logs },
  { path: '/logs/diagnostics', component: Diagnostics },
  { path: '/settings', component: Settings },
  { path: '/settings/:section', component: Settings },
  { path: '/welcome', component: Welcome, bare: true },
  { path: '/login', component: Login, bare: true },
  { path: '/_design', component: Design },
];

export const BARE_PATHS = ROUTES.filter((r) => r.bare).map((r) => r.path);

/** Preloads the Status chunk alongside the entry so the first paint needs no extra round trip. */
export function preloadInitial(): void {
  void Status.preload();
}
