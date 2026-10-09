import { useEffect, useRef } from 'preact/hooks';

/** g-chord targets. */
export const GOTO: Readonly<Record<string, string>> = {
  s: '/status',
  m: '/models',
  c: '/chat',
  t: '/tools/playground',
  i: '/integrations',
  l: '/logs',
  ',': '/settings',
  h: '/status/history',
  d: '/models/downloader',
};

export const CHORD_TIMEOUT_MS = 1500;

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

function sheetOpen(): boolean {
  return document.querySelector('[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]') !== null;
}

export interface ShortcutHandlers {
  navigate: (to: string) => void;
  toggleTheme: () => void;
  openHelp: () => void;
}

/**
 * Global keyboard shortcuts: `g` chords, `/` (page search), `?` (help), `t` (theme).
 * Inactive while typing in a field or while a sheet is open. Never destructive.
 */
export function useShortcuts(handlers: ShortcutHandlers): void {
  const ref = useRef(handlers);
  ref.current = handlers;
  useEffect(() => {
    let chordAt = 0;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTyping(e.target) || sheetOpen()) return;
      const now = Date.now();
      if (chordAt && now - chordAt < CHORD_TIMEOUT_MS) {
        chordAt = 0;
        const to = GOTO[e.key];
        if (to) {
          e.preventDefault();
          ref.current.navigate(to);
        }
        return;
      }
      chordAt = 0;
      if (e.key === 'g') {
        chordAt = now;
      } else if (e.key === '?') {
        e.preventDefault();
        ref.current.openHelp();
      } else if (e.key === '/') {
        const search = document.querySelector<HTMLElement>('[data-primary-search="true"]');
        if (search) {
          e.preventDefault();
          search.focus();
        }
      } else if (e.key === 't') {
        ref.current.toggleTheme();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
}
