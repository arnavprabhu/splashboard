import { Link, useLocation } from 'wouter-preact';
import type { EngineState } from '../api/types';
import { effectiveTheme, toggleTheme } from '../store/theme';
import { t } from '../strings/en';
import { StatusChip } from './StatusChip';

export interface NavItem {
  href: string;
  label: string;
  /** Prefix that marks the link active; defaults to `href`. */
  match?: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { href: '/status', label: t('nav.status') },
  { href: '/models', label: t('nav.models') },
  { href: '/chat', label: t('nav.chat') },
  { href: '/tools/playground', label: t('nav.tools'), match: '/tools' },
  { href: '/integrations', label: t('nav.integrations') },
  { href: '/logs', label: t('nav.logs') },
  { href: '/settings', label: t('nav.settings') },
];

export function isActive(location: string, item: Pick<NavItem, 'href' | 'match'>): boolean {
  const prefix = item.match ?? item.href;
  return location === prefix || location.startsWith(`${prefix}/`);
}

export interface NavBandProps {
  engineVersion?: string | null;
  model?: string | null;
  state?: EngineState | null;
  authEnabled?: boolean;
  onLogout?: () => void;
  /** "74 tok/s" while busy (decision S1). */
  tokps?: string | null;
  /** "42%" while a download runs (decision S2). */
  modelsProgress?: string | null;
  /** Splash is not installed: show only Settings (docs/ui/01 §1.3). */
  engineMissing?: boolean;
  /** Logged out: no links, only the toggle (docs/ui/01 §6). */
  linksHidden?: boolean;
}

export function ThemeToggle() {
  const dark = effectiveTheme.value === 'dark';
  return (
    <button
      type="button"
      class="theme-toggle nav"
      onClick={toggleTheme}
      aria-label={dark ? t('nav.theme_to_light') : t('nav.theme_to_dark')}
      data-testid="theme-toggle"
    >
      {dark ? t('nav.theme_light') : t('nav.theme_dark')}
    </button>
  );
}

/** Wordmark · meta (engine version · model · state chip) · text links · theme toggle. */
export function NavBand({ engineVersion, model, state, authEnabled, onLogout, tokps, modelsProgress, engineMissing, linksHidden }: NavBandProps) {
  const [location] = useLocation();
  const items = linksHidden ? [] : engineMissing ? NAV_ITEMS.filter((i) => i.href === '/settings') : NAV_ITEMS;
  return (
    <header class="navband">
      <Link href="/status" class="navband-mark" aria-label="Splash GUI, status">
        Splash GUI
      </Link>
      <div class="navband-meta meta" aria-label="Engine">
        {/* Each separator trails its item inside one group, so a wrapped line never starts with "·". */}
        <span class="navband-item">
          <span>{engineMissing ? t('nav.engine_missing') : engineVersion ? t('nav.engine', { version: engineVersion }) : t('nav.engine_unknown')}</span>
          <span aria-hidden="true">·</span>
        </span>
        <span class="navband-item">
          <span class="mono">{model ?? t('nav.no_model')}</span>
          <span aria-hidden="true">·</span>
        </span>
        <span class="navband-item">
          <StatusChip state={state ?? null} />
          {tokps && <span aria-hidden="true">·</span>}
        </span>
        {tokps && (
          <span class="tnum" data-testid="nav-tokps">
            {tokps}
          </span>
        )}
      </div>
      <nav aria-label={t('nav.main')}>
        <ul class="navband-links">
          {items.map((item) => {
            const active = isActive(location, item);
            return (
              <li key={item.href}>
                <Link href={item.href} class="navlink nav" aria-current={active ? 'page' : undefined}>
                  {item.label}
                  {item.href === '/models' && modelsProgress && <span class="tnum navlink-meta"> {modelsProgress}</span>}
                </Link>
              </li>
            );
          })}
          {authEnabled && !linksHidden && (
            <li>
              <button type="button" class="navlink nav" onClick={onLogout}>
                {t('nav.logout')}
              </button>
            </li>
          )}
          <li>
            <ThemeToggle />
          </li>
        </ul>
      </nav>
    </header>
  );
}
