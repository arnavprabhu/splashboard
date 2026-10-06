/**
 * The chat workspace's left column: the wordmark (back to the admin page this tab came from),
 * the engine version, the conversation list, and links to the other admin screens.
 */
import type { ComponentChildren } from 'preact';
import { Link } from 'wouter-preact';
import { Button } from '../../components/Button';
import { effectiveTheme } from '../../store/theme';
import { t } from '../../strings/chat';

/** The wordmark link: the nav band's treatment, sized for the workspace. */
export function HomeLink({ href }: { href: string }) {
  return (
    <Link href={href} class="navband-mark chat-home" aria-label={t('chat.home_label')} data-testid="chat-home">
      {t('chat.home')}
    </Link>
  );
}

/**
 * The theme toggle the hidden nav band carries, mirrored here. It clicks the nav band's own
 * toggle (still mounted, only hidden) so the initial bundle exports nothing new (SPEC §18.6).
 */
function ThemeButton() {
  const dark = effectiveTheme.value === 'dark';
  return (
    <button
      type="button"
      class="theme-toggle nav"
      aria-label={dark ? t('nav.theme_to_light') : t('nav.theme_to_dark')}
      data-testid="chat-theme-toggle"
      onClick={() => document.querySelector<HTMLButtonElement>('.navband .theme-toggle')?.click()}
    >
      {dark ? t('nav.theme_light') : t('nav.theme_dark')}
    </button>
  );
}

/** Settings · Models · Downloads, and the theme toggle the hidden nav band would carry. */
export function AdminLinks() {
  return (
    <div class="chat-side-foot">
      <nav aria-label={t('chat.side.links')}>
        <ul class="chat-side-links">
          <li>
            <Link href="/settings" class="nav">
              {t('chat.side.settings')}
            </Link>
          </li>
          <li>
            <Link href="/models" class="nav">
              {t('chat.side.models')}
            </Link>
          </li>
          <li>
            <Link href="/models/downloader" class="nav">
              {t('chat.side.downloads')}
            </Link>
          </li>
        </ul>
      </nav>
      <ThemeButton />
    </div>
  );
}

export interface SidebarProps {
  home: string;
  version: string | null;
  onHide: () => void;
  children: ComponentChildren;
}

export function Sidebar({ home, version, onHide, children }: SidebarProps) {
  return (
    <aside class="chat-side" id="chat-side" aria-label={t('chat.side.label')}>
      <div class="chat-side-head">
        <div class="chat-side-brand">
          <HomeLink href={home} />
          {version && <span class="meta chat-side-version">{version}</span>}
        </div>
        <Button size="s" variant="text" aria-label={t('chat.side.hide_label')} aria-expanded="true" aria-controls="chat-side" data-chat-toggle="list-hide" onClick={onHide}>
          {t('chat.side.hide')}
        </Button>
      </div>
      {children}
      <AdminLinks />
    </aside>
  );
}
