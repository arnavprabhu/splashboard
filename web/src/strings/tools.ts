/** Tools strings (docs/ui). Keys start with "tools.". Common keys come from ./en. */
import { en } from './en';
import { makeT } from './index';

export const toolsStrings = {
  'tools.page_title': 'Tools',
} as const;

export const t = makeT({ ...en, ...toolsStrings });
