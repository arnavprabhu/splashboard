/**
 * Native host bridge for the menu bar app's Welcome window.
 * The contract is the one macos/Sources/SplashGUIKit/Bridge/WelcomeBridge.swift implements:
 *
 *   JS → native: window.webkit.messageHandlers.splashGUI.postMessage({ type, requestId, ...payload })
 *   native → JS: window.splashGUIHost.resolve(requestId, result)
 *
 * The bridge is used only when the page was opened with `?host=app` AND the handler exists;
 * everywhere else the wizard falls back to web behaviour (typed paths, copyable commands).
 */

export type HostFeature = 'pickFolder' | 'openHomebrewInstaller' | 'openTerminal' | 'openURL' | 'closeWelcome';

export interface HostResult {
  ok?: boolean;
  path?: string;
  cancelled?: boolean;
  error?: string;
  version?: string;
  features?: string[];
  [extra: string]: unknown;
}

interface MessageHandler {
  postMessage: (message: unknown) => void;
}

interface HostWindow {
  location?: { search: string };
  webkit?: { messageHandlers?: { splashGUI?: MessageHandler } };
  splashGUIHost?: { resolve: (requestId: string, result: unknown) => void };
}

export class HostError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'HostError';
  }
}

/** Default reply timeout; a folder picker waits for the user, so it gets much longer. */
export const HOST_TIMEOUT_MS = 10_000;
export const PICK_TIMEOUT_MS = 10 * 60_000;
/** `closeWelcome` may never answer (the window closes); we stop waiting quickly. */
export const CLOSE_TIMEOUT_MS = 1_500;

function win(): HostWindow {
  return globalThis as unknown as HostWindow;
}

function handler(w: HostWindow = win()): MessageHandler | null {
  return w.webkit?.messageHandlers?.splashGUI ?? null;
}

/** `?host=app` in the URL (the app's welcome window), whether or not the handler exists. */
export function hostParam(search: string = win().location?.search ?? ''): boolean {
  return new URLSearchParams(search).get('host') === 'app';
}

/** True when the native bridge can be used: `?host=app` and the message handler is present. */
export function isHosted(search?: string, w: HostWindow = win()): boolean {
  return hostParam(search ?? w.location?.search ?? '') && handler(w) !== null;
}

interface Pending {
  resolve: (result: HostResult) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

const pending = new Map<string, Pending>();
let counter = 0;

function readResult(v: unknown): HostResult {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as HostResult) : { ok: v === true };
}

/** The native side calls this with the reply to a message. Unknown ids are ignored. */
export function resolveHost(requestId: string, result: unknown): void {
  const entry = pending.get(String(requestId));
  if (!entry) return;
  pending.delete(String(requestId));
  clearTimeout(entry.timer);
  entry.resolve(readResult(result));
}

/** Installs `window.splashGUIHost = { resolve }` (idempotent). */
export function installHost(w: HostWindow = win()): void {
  if (w.splashGUIHost?.resolve === resolveHost) return;
  w.splashGUIHost = { resolve: resolveHost };
}

/**
 * Posts one message and waits for its reply. Rejects with HostError when the bridge is
 * missing, the native side reports `{error}`, or nothing answers within `timeoutMs`.
 */
export function hostCall(type: string, payload: Record<string, unknown> = {}, timeoutMs = HOST_TIMEOUT_MS, w: HostWindow = win()): Promise<HostResult> {
  const h = handler(w);
  if (!h) return Promise.reject(new HostError('The app bridge is not available.'));
  installHost(w);
  counter += 1;
  const requestId = `wz-${Date.now().toString(36)}-${counter}`;
  return new Promise<HostResult>((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(requestId);
      reject(new HostError(`No answer from the app (${type}).`));
    }, timeoutMs);
    pending.set(requestId, {
      resolve: (result) => (typeof result.error === 'string' && result.error ? reject(new HostError(result.error)) : resolve(result)),
      reject,
      timer,
    });
    try {
      h.postMessage({ ...payload, type, requestId });
    } catch (err) {
      clearTimeout(timer);
      pending.delete(requestId);
      reject(new HostError(err instanceof Error ? err.message : String(err)));
    }
  });
}

/** Number of messages still waiting for a reply (tests). */
export function pendingCount(): number {
  return pending.size;
}

// ---------- typed wrappers ----------

export function hello(): Promise<HostResult> {
  return hostCall('hello');
}

/** Native folder picker. Resolves to the chosen path, or null when the user cancelled. */
export async function pickFolder(target: 'models' | 'cache', current?: string | null): Promise<string | null> {
  const result = await hostCall('pickFolder', current ? { target, current } : { target }, PICK_TIMEOUT_MS);
  if (result.cancelled) return null;
  if (typeof result.path === 'string' && result.path) return result.path;
  throw new HostError('The app returned no folder.');
}

export async function openHomebrewInstaller(): Promise<void> {
  await hostCall('openHomebrewInstaller');
}

/** Runs an allow-listed command in Terminal (TerminalCommandPolicy in the app). */
export async function openTerminal(command: string): Promise<void> {
  await hostCall('openTerminal', { command });
}

export async function openURL(url: string): Promise<void> {
  await hostCall('openURL', { url });
}

/** Asks the app to close the welcome window. The reply may never come; that is fine. */
export async function closeWelcome(completed: boolean): Promise<void> {
  try {
    await hostCall('closeWelcome', { completed }, CLOSE_TIMEOUT_MS);
  } catch (err) {
    if (err instanceof HostError && err.message.startsWith('No answer')) return;
    throw err;
  }
}

/** The commands the app's TerminalCommandPolicy allows (macOS WelcomeBridge.swift). */
export const HOMEBREW_INSTALL_COMMAND = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"';
export const SPLASH_INSTALL_COMMAND = 'brew install incoai/tap/splash';
export const SPLASH_UPGRADE_COMMAND = 'brew upgrade incoai/tap/splash';
