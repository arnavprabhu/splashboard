import { cleanup } from '@testing-library/preact';
import { afterEach, vi } from 'vitest';

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
