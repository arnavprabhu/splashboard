/**
 * What Splash 1.4.1 adds on screen: Flint and Ornith-1.5-122B in the offline catalog, a
 * sliding-window target's pages in the Cache band, and Chat's note for a model that decides
 * itself when to reason.
 */
import { render, screen } from '@testing-library/preact';
import { beforeEach, describe, expect, it } from 'vitest';
import { catalogSeed } from '../src/routes/models/logic';
import { CacheBand } from '../src/routes/status/Bands';
import { StartupBand } from '../src/routes/status/Startup';
import { settings } from '../src/store';
import { liveSample } from '../src/store/live';
import { t } from '../src/strings/chat';

beforeEach(() => {
  settings.value = { settings: { version: 1, global: {}, models: {} } } as never;
  liveSample.value = null;
});

describe('offline catalog', () => {
  it('lists Flint, Ornith-1.5-122B and the pruned Flash-Next', () => {
    const families = Object.fromEntries(catalogSeed().families.map((f) => [f.family, f]));
    expect(Object.keys(families)).toEqual(['Qwen3.8-27B', 'Qwen3.6-35B-A3B', 'Flint', 'Qwen3.5-122B-A10B', 'Qwen3.8-Flash-Next']);
    expect(families['Flint']!.groups.map((g) => g.format)).toEqual(['gguf']);
    expect(families['Flint']!.groups[0]!.entries[0]!.notes).toContain('depends a lot on your use');
    expect(families['Qwen3.5-122B-A10B']!.groups.map((g) => g.format)).toEqual(['mlx', 'gguf']);
    const flash = families['Qwen3.8-Flash-Next']!.groups.flatMap((g) => g.entries.map((e) => e.id));
    expect(flash).toContain('ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF');
  });
});

describe('Cache band', () => {
  const window = { window_pages: 16, trim: true, full_pages: 20, slim_pages: 10, slim_bytes: 4_730_880, pinned_blocks: 16, trimmed_pages: 12, upgrades: 0, refused_states: 0 };

  it('shows a sliding-window target’s full and slim pages', () => {
    const { container } = render(<CacheBand raw={{ kv: { pages_active: 30, window } }} stopped={false} />);
    expect(container.textContent).toContain('Sliding window · 20 full pages, 10 slim pages in 4.5 MB · 12 trimmed');
  });

  it('says when trimming is off and shows nothing for other targets', () => {
    const off = render(<CacheBand raw={{ kv: { window: { ...window, trim: false } } }} stopped={false} />);
    expect(off.container.textContent).toContain('Sliding window · 20 full pages · trimming off');
    off.unmount();
    const plain = render(<CacheBand raw={{ kv: { pages_active: 3 } }} stopped={false} />);
    expect(plain.container.textContent).not.toContain('Sliding window');
  });
});

describe('Chat', () => {
  it('explains that reasoning settings change nothing for Flint', () => {
    render(<p>{t('chat.help.reasoning_model_decides')}</p>);
    expect(screen.getByText(/decides each turn whether to reason/)).toBeTruthy();
  });
});

describe('startup', () => {
  it('says when Splash waits for memory to free up before it starts', () => {
    const message = '10:00:00 Waiting for sufficient available memory to start; the macOS reserve remains protected.';
    const engine = {
      state: 'starting',
      phase: 'loading',
      model: 'unsloth/flint-GGUF:UD-Q4_K_XL',
      view: { notices: [{ ts: '2026-10-26T10:00:00Z', kind: 'memory_wait', message }], log_tail: [message] },
    } as never;
    render(<StartupBand engine={engine} />);
    expect(screen.getByText('Waiting for memory to free up')).toBeTruthy();
  });
});
