/** Sign-in strings (docs/ui/01 §6, D58). Keys start with "login.". Kept apart from ./en so the
 * initial bundle does not carry them (SPEC §18.6); the login route loads lazily. */
import { en } from './en';
import { makeT } from './index';

export const loginStrings = {
  'login.page_title': 'Sign in',
  'login.title': 'Sign in.',
  'login.lead': 'Enter the API key to open Splash GUI.',
  'login.key': 'API key',
  'login.submit': 'Sign in',
  'login.checking': 'Checking…',
  'login.help': 'The key is in Settings → Security on the Mac that runs Splash GUI, or from {cmd}.',
  'login.menubar': 'On the Mac that runs Splash GUI, Open Admin Panel in the menu bar signs you in without the key.',
  'login.wrong': 'That key didn’t match.',
  'login.throttled': 'Too many attempts. Try again in {s} s.',
  'login.throttled_soon': 'Too many attempts. Try again in a moment.',
  'login.unreachable': 'Splash GUI is not answering. Is it running?',
  'login.failed': 'Could not sign in.',
  'login.expired': 'Your session expired. Sign in again.',
  'login.expired_settings': 'Your session expired. Sign in again; unsaved settings changes are kept while this tab stays open.',
  'login.link_signing_in': 'Signing you in…',
  'login.link_expired': 'This sign-in link has expired or was already used. Links work once, within a minute. Open the admin again from the menu bar, or sign in with the API key.',
  'login.write_needs_session': 'Sign in to make changes. Reading is open on this Mac, but changes and secrets need the API key.',
  'login.write_needs_session_settings': 'Sign in to make changes. Reading is open on this Mac, but changes and secrets need the API key. Unsaved settings changes are kept while this tab stays open.',
} as const;

export const t = makeT({ ...en, ...loginStrings });
