import { Link, useLocation } from 'wouter-preact';
import type { EngineState } from '../api/types';
import { effectiveTheme, toggleTheme } from '../store/theme';
import { StatusChip } from './StatusChip';

export interface NavItem {
  href: string;
  label: string;
  /** Prefix that marks the link active; defaults to `href`. */
  match?: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { href: '/status', label: 'Status' },
  { href: '/models', label: 'Models' },
  { href: '/chat', label: 'Chat' },
  { href: '/tools/playground', label: 'Tools', match: '/tools' },
  { href: '/integrations', label: 'Integrations' },
  { href: '/logs', label: 'Logs' },
  { href: '/settings', label: 'Settings' },
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
}

export function ThemeToggle() {
  const dark = effectiveTheme.value === 'dark';
  return (
    <button
      type="button"
      class="theme-toggle nav"
      onClick={toggleTheme}
      aria-label={dark ? 'Switch to light theme' : 'Switch to dark theme'}
      data-testid="theme-toggle"
    >
      {dark ? '☀ Light' : '☾ Dark'}
    </button>
  );
}

/** Wordmark · meta (engine version · model · state chip) · text links · theme toggle. */
export function NavBand({ engineVersion, model, state, authEnabled, onLogout }: NavBandProps) {
  const [location] = useLocation();
  return (
    <header class="navband">
      <Link href="/status" class="navband-mark" aria-label="Splash GUI, status">
        Splash GUI
      </Link>
      <div class="navband-meta meta" aria-label="Engine">
        <span>{engineVersion ? `Splash ${engineVersion}` : 'Splash —'}</span>
        <span aria-hidden="true">·</span>
        <span class="mono" style={{ overflowWrap: 'anywhere' }}>
          {model ?? 'No model'}
        </span>
        <span aria-hidden="true">·</span>
        <StatusChip state={state ?? null} />
      </div>
      <nav aria-label="Main">
        <ul class="navband-links">
          {NAV_ITEMS.map((item) => {
            const active = isActive(location, item);
            return (
              <li key={item.href}>
                <Link href={item.href} class="navlink nav" aria-current={active ? 'page' : undefined}>
                  {item.label}
                </Link>
              </li>
            );
          })}
          {authEnabled && (
            <li>
              <button type="button" class="navlink nav" onClick={onLogout}>
                Log out
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
