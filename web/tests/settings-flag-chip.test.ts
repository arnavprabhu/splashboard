/**
 * Settings field flag chip: the Splash flag or env var is shown; a manager-only
 * setting has neither and shows no chip (its key stays in the "?" disclosure).
 */
import { describe, expect, it } from 'vitest';
import type { SchemaField } from '../src/api/models';
import { flagText } from '../src/routes/settings/SettingField';

const field = (over: Partial<SchemaField>) => ({ key: 'x.y', label: 'X', ...over }) as unknown as SchemaField;

describe('flagText', () => {
  it('shows the Splash flag verbatim', () => {
    expect(flagText(field({ key: 'serve.max_cache_disk', flag: '--max-cache-disk' }))).toBe('--max-cache-disk');
  });

  it('shows the env var when a setting has one and no flag', () => {
    expect(flagText(field({ key: 'advanced.crash_trace', env: 'SPLASH_CRASH_TRACE' }))).toBe('env SPLASH_CRASH_TRACE');
  });

  it('shows no chip for a manager-only setting such as a notification toggle', () => {
    expect(flagText(field({ key: 'notifications.download_done', flag: null, env: null }))).toBeUndefined();
    expect(flagText(field({ key: 'menubar.show_tokps' }))).toBeUndefined();
  });
});
