/** Integrations strings (docs/ui). Keys start with "integrations.". Common keys come from ./en. */
import { en } from './en';
import { makeT } from './index';

export const integrationsStrings = {
  'integrations.page_title': 'Integrations',
} as const;

export const t = makeT({ ...en, ...integrationsStrings });
