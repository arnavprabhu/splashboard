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

describe('pre-paint theme script (index.html)', () => {
  const fsName = 'node:fs';
  async function script(): Promise<string> {
    const { readFileSync } = (await import(/* @vite-ignore */ fsName)) as { readFileSync: (p: string, e: string) => string };
    const html = readFileSync(`${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/index.html`, 'utf8');
    const m = /<script>([\s\S]*?)<\/script>/.exec(html);
    if (!m?.[1]) throw new Error('inline theme script not found');
    return m[1];
  }
  function run(code: string, stored: string | null, search: string): string | null {
    const root = { theme: null as string | null, setAttribute: (_: string, v: string) => (root.theme = v) };
    const storage = { getItem: () => stored };
    new Function('localStorage', 'location', 'document', code)(storage, { search }, { documentElement: root });
    return root.theme;
  }

  it('uses the stored choice first', async () => {
    const code = await script();
    expect(run(code, 'dark', '')).toBe('dark');
    expect(run(code, 'light', '?host=app&theme=dark')).toBe('light');
  });

  it("falls back to the menu bar app's ?theme= (docs/ui/01 §7), then light", async () => {
    const code = await script();
    expect(run(code, null, '?host=app&theme=dark')).toBe('dark');
    expect(run(code, null, '?theme=light&host=app')).toBe('light');
    expect(run(code, null, '?theme=purple')).toBe('light');
    expect(run(code, null, '')).toBe('light');
  });
});
