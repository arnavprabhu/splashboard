import { computed, effect, signal } from '@preact/signals';

export type ThemePref = 'light' | 'dark' | 'system';
export type Theme = 'light' | 'dark';

/** Shared with the inline pre-paint script in index.html. */
export const THEME_STORAGE_KEY = 'splash-gui-theme';

export function isThemePref(value: unknown): value is ThemePref {
  return value === 'light' || value === 'dark' || value === 'system';
}

function storedPref(): ThemePref | null {
  try {
    const stored = globalThis.localStorage?.getItem(THEME_STORAGE_KEY);
    if (isThemePref(stored)) return stored;
  } catch {
    /* storage can be blocked; fall through */
  }
  return null;
}

function readStored(): ThemePref {
  const stored = storedPref();
  if (stored) return stored;
  const attr = globalThis.document?.documentElement.getAttribute('data-theme');
  return isThemePref(attr) ? attr : 'light';
}

function systemQuery(): MediaQueryList | null {
  return typeof globalThis.matchMedia === 'function' ? globalThis.matchMedia('(prefers-color-scheme: dark)') : null;
}

export const themePref = signal<ThemePref>(readStored());
export const systemPrefersDark = signal<boolean>(systemQuery()?.matches ?? false);

export const effectiveTheme = computed<Theme>(() =>
  themePref.value === 'system' ? (systemPrefersDark.value ? 'dark' : 'light') : themePref.value,
);

export function setTheme(pref: ThemePref): void {
  themePref.value = pref;
  try {
    globalThis.localStorage?.setItem(THEME_STORAGE_KEY, pref);
  } catch {
    /* ignore */
  }
}

/**
 * Applies settings.json `ui.theme` as the default for browsers that
 * have no stored choice yet. A choice made in this browser always wins.
 */
export function adoptThemeDefault(value: unknown): void {
  if (!isThemePref(value) || storedPref() !== null) return;
  themePref.value = value;
}

/** The nav toggle flips between explicit light and dark. */
export function toggleTheme(): void {
  setTheme(effectiveTheme.value === 'dark' ? 'light' : 'dark');
}

let installed = false;

/** Mirrors the preference onto <html data-theme> and tracks the OS setting. */
export function installTheme(): () => void {
  if (installed) return () => undefined;
  installed = true;
  const mq = systemQuery();
  const onChange = (e: MediaQueryListEvent) => {
    systemPrefersDark.value = e.matches;
  };
  mq?.addEventListener('change', onChange);
  const stop = effect(() => {
    document.documentElement.setAttribute('data-theme', themePref.value);
  });
  return () => {
    installed = false;
    mq?.removeEventListener('change', onChange);
    stop();
  };
}
