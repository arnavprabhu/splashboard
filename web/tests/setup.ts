import { cleanup } from '@testing-library/preact';
import { afterAll, afterEach, vi } from 'vitest';

vi.mock('uplot', () => ({ default: class { static paths = {}; setData() {} setSize() {} destroy() {} } }));
if (typeof EventSource === 'undefined') {
  Object.assign(globalThis, { EventSource: class { static CLOSED = 2; readyState = 0; addEventListener() {} close() {} } });
}

if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// Unmounting schedules Preact's remaining effect cleanups on a timer. Let them run before the
// file's jsdom is torn down, or they fire into a closed window ("document is not defined").
afterAll(async () => {
  vi.useRealTimers();
  await new Promise((resolve) => setTimeout(resolve, 150));
});
