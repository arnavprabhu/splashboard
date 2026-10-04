import { describe, expect, it } from 'vitest';
import { effectiveTheme, installTheme, isThemePref, setTheme, systemPrefersDark, THEME_STORAGE_KEY, themePref, toggleTheme } from '../src/store/theme';

describe('theme', () => {
  it('persists, mirrors onto <html>, and toggles explicit themes', () => {
    const stop = installTheme();
    setTheme('dark');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
    toggleTheme();
    expect(themePref.value).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    stop();
  });

  it('follows the OS when set to system', () => {
    setTheme('system');
    systemPrefersDark.value = true;
    expect(effectiveTheme.value).toBe('dark');
    toggleTheme();
    expect(themePref.value).toBe('light');
    systemPrefersDark.value = false;
  });

  it('survives blocked storage', () => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = () => {
      throw new Error('blocked');
    };
    try {
      expect(() => setTheme('dark')).not.toThrow();
      expect(themePref.value).toBe('dark');
    } finally {
      Storage.prototype.setItem = original;
    }
  });

  it('validates preferences', () => {
    expect(isThemePref('system')).toBe(true);
    expect(isThemePref('sepia')).toBe(false);
  });
});
