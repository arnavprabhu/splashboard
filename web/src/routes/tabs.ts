import type { NavItem } from '../components/NavBand';

export const STATUS_TABS: readonly NavItem[] = [
  { href: '/status', label: 'Live' },
  { href: '/status/history', label: 'Usage history' },
];

export const MODELS_TABS: readonly NavItem[] = [
  { href: '/models', label: 'Manager' },
  { href: '/models/downloader', label: 'Downloader' },
];

export const LOGS_TABS: readonly NavItem[] = [
  { href: '/logs', label: 'Live tail' },
  { href: '/logs/diagnostics', label: 'Diagnostics' },
];

/** Settings sections in SPEC §10.9 order; slugs are the /admin/settings/:section values. */
export const SETTINGS_SECTIONS = [
  { slug: 'server', label: 'Server & network' },
  { slug: 'security', label: 'Security' },
  { slug: 'storage', label: 'Models & storage' },
  { slug: 'memory', label: 'Memory & context' },
  { slug: 'cache', label: 'Cache' },
  { slug: 'performance', label: 'Performance' },
  { slug: 'requests', label: 'Requests & limits' },
  { slug: 'sampling', label: 'Reasoning & sampling' },
  { slug: 'routing', label: 'Routing' },
  { slug: 'hf', label: 'Hugging Face' },
  { slug: 'chat', label: 'Chat & MCP' },
  { slug: 'lifecycle', label: 'Lifecycle' },
  { slug: 'menubar', label: 'Menu bar' },
  { slug: 'notifications', label: 'Notifications' },
  { slug: 'data', label: 'Data & privacy' },
  { slug: 'advanced', label: 'Advanced' },
  { slug: 'about', label: 'About' },
] as const;

export type SettingsSlug = (typeof SETTINGS_SECTIONS)[number]['slug'];
