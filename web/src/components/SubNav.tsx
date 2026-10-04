import { Link, useLocation } from 'wouter-preact';
import { isActive, type NavItem } from './NavBand';

export const TOOLS_TABS: readonly NavItem[] = [
  { href: '/tools/playground', label: 'Playground' },
  { href: '/tools/tokenizer', label: 'Tokenizer' },
  { href: '/tools/judgments', label: 'Judgments' },
  { href: '/tools/benchmark', label: 'Benchmark' },
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
