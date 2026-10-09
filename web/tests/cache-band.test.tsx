import { render, screen } from '@testing-library/preact';
import { beforeEach, describe, expect, it } from 'vitest';
import { CacheBand } from '../src/routes/status/Bands';
import { settings } from '../src/store';

const PERSISTENT = { disk: { persistent: true, capacity_bytes: 1024 } };

function useCacheDir(resolved: unknown) {
  settings.value = {
    settings: { version: 1, global: {}, models: {} },
    ...(resolved === undefined ? {} : { resolved }),
  } as never;
}

beforeEach(() => {
  useCacheDir(undefined);
});

describe('Status → Cache band, persistent folder', () => {
  it('shows the resolved cache folder with the home prefix shortened to ~', () => {
    useCacheDir({ home: '/Users/arnav', cache_dir: '/Users/arnav/Library/Scratch/cache' });
    render(<CacheBand raw={PERSISTENT} stopped={false} />);
    expect(screen.getByText('On · ~/Library/Scratch/cache')).toBeTruthy();
  });

  it('keeps another user\'s folder under /Users as it is', () => {
    useCacheDir({ home: '/Users/bob', cache_dir: '/Users/Shared/cache' });
    render(<CacheBand raw={PERSISTENT} stopped={false} />);
    expect(screen.getByText('On · /Users/Shared/cache')).toBeTruthy();
  });

  it('keeps a sibling folder whose name starts with the home name as it is', () => {
    useCacheDir({ home: '/Users/bob', cache_dir: '/Users/bob2/cache' });
    render(<CacheBand raw={PERSISTENT} stopped={false} />);
    expect(screen.getByText('On · /Users/bob2/cache')).toBeTruthy();
  });

  it('shows a folder outside the home directory as it is', () => {
    useCacheDir({ cache_dir: '/Volumes/Fast SSD/splash/cache' });
    render(<CacheBand raw={PERSISTENT} stopped={false} />);
    expect(screen.getByText('On · /Volumes/Fast SSD/splash/cache')).toBeTruthy();
  });

  it('falls back to the default folder only when the API gives none', () => {
    render(<CacheBand raw={PERSISTENT} stopped={false} />);
    expect(screen.getByText('On · ~/.splash/cache')).toBeTruthy();
  });
});
