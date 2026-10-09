import { Link, useLocation } from 'wouter-preact';
import { isActive, type NavItem } from './NavBand';
import { t } from '../strings/en';

export const TOOLS_TABS: readonly NavItem[] = [
  { href: '/tools/playground', label: t('nav.tab.playground') },
  { href: '/tools/tokenizer', label: t('nav.tab.tokenizer') },
  { href: '/tools/judgments', label: t('nav.tab.judgments') },
  { href: '/tools/benchmark', label: t('nav.tab.benchmark') },
];

export interface SubNavProps {
  items: readonly NavItem[];
  label: string;
  /** Exact matching, for tab sets where one tab is a prefix of another. */
  exact?: boolean;
}

/** A secondary text band of tabs under the nav (Tools, Models, Status, Logs). */
export function SubNav({ items, label, exact }: SubNavProps) {
  const [location] = useLocation();
  return (
    <nav class="subnav" aria-label={label}>
      {items.map((item) => {
        const active = exact ? location === (item.match ?? item.href) : isActive(location, item);
        return (
          <Link key={item.href} href={item.href} class="navlink nav" aria-current={active ? 'page' : undefined}>
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
