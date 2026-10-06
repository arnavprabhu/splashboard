/**
 * Where the chat workspace's logo goes: the admin page this tab came from. Lives in the chat
 * chunk so the shell (initial bundle) does not have to track routes.
 *
 * Sources, newest first: this tab's history entries before the current one (Navigation API,
 * same-origin only), then `document.referrer`, then what an earlier visit of this tab remembered
 * in sessionStorage, then Status.
 */
const KEY = 'chat.return';
export const FALLBACK = '/status';

/** Admin pages the logo never returns to: the chat itself and full-page flows. */
const SKIP = /^\/(chat|login|welcome)(\/|$|\?)/;

/** `/admin/models?x=1` → `/models?x=1` when it is a same-origin admin page worth returning to. */
export function adminPath(url: string | null | undefined, base: string, origin: string): string | null {
  if (!url) return null;
  let u: URL;
  try {
    u = new URL(url, origin);
  } catch {
    return null;
  }
  if (u.origin !== origin || !u.pathname.startsWith(`${base}/`)) return null;
  const path = u.pathname.slice(base.length) + u.search;
  if (path === '/' || SKIP.test(path)) return null;
  return path;
}

interface NavLike {
  currentEntry?: { index: number } | null;
  entries?: () => Array<{ url: string | null; index: number }>;
}

/** Same-origin history entries before the current one, newest first. */
function historyUrls(): string[] {
  const nav = (globalThis as { navigation?: NavLike }).navigation;
  try {
    const cur = nav?.currentEntry?.index;
    if (!nav?.entries || cur == null) return [];
    return nav
      .entries()
      .filter((e) => e.index < cur)
      .sort((a, b) => b.index - a.index)
      .map((e) => e.url ?? '');
  } catch {
    return [];
  }
}

function readStored(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/** The path (relative to the router base) the logo links to; remembered for this tab. */
export function adminReturnPath(base: string, sources: { history?: string[]; referrer?: string; stored?: string | null } = {}): string {
  const origin = globalThis.location?.origin ?? 'http://localhost';
  const candidates = [...(sources.history ?? historyUrls()), sources.referrer ?? globalThis.document?.referrer ?? ''];
  let found: string | null = null;
  for (const url of candidates) {
    found = adminPath(url, base, origin);
    if (found) break;
  }
  const stored = sources.stored !== undefined ? sources.stored : readStored();
  const path = found ?? (stored ? adminPath(`${base}${stored}`, base, origin) : null) ?? FALLBACK;
  try {
    sessionStorage.setItem(KEY, path);
  } catch {
    /* per-tab convenience only */
  }
  return path;
}
