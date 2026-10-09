/**
 * Gap 10.2 step 1: a Mac Splash cannot run gets a stop state with its reason and the action that
 * can fix it (docs/ui/04 §7, SPEC §10.2), not a disabled Continue.
 */
import { fireEvent, render, screen } from '@testing-library/preact';
import { describe, expect, it, vi } from 'vitest';
import type { SystemInfo } from '../src/api/models';
import { StopState, REQUIREMENTS_URL, SOFTWARE_UPDATE_URL } from '../src/routes/welcome/StepStop';
import { stopReason } from '../src/routes/welcome/logic';

const VOL = { path: '/Users/a/.splash/models', total_bytes: 1e12, free_bytes: 3e11 };

function system(over: Partial<SystemInfo>): SystemInfo {
  return {
    chip: 'Apple M5 Pro',
    memory_bytes: 68_719_476_736,
    macos_version: '27.0',
    arch: 'arm64',
    hostname: 'mac',
    supported: true,
    unsupported_reasons: [],
    disk: { models: VOL, cache: VOL },
    power: { source: 'ac' },
    ...over,
  } as unknown as SystemInfo;
}

const M2 = system({ chip: 'Apple M2 Pro', supported: false, unsupported_reasons: ['Splash needs an M3 or newer; this Mac has an Apple M2 Pro'] });
const OLD_MACOS = system({ macos_version: '26.1', supported: false, unsupported_reasons: ['Splash needs macOS 26.4 or newer; this Mac runs 26.1'] });

describe('which stop state applies', () => {
  it('an M2 is a chip stop; an Intel Mac is a chip stop too', () => {
    expect(stopReason(M2)?.kind).toBe('chip');
    expect(stopReason(system({ chip: 'Intel(R) Core(TM) i7', arch: 'x86_64', supported: false, unsupported_reasons: ['Splash needs an Apple silicon Mac (M3 or newer)'] }))?.kind).toBe('chip');
  });
  it('an old macOS on a supported chip is the macOS stop, with the version it runs', () => {
    expect(stopReason(OLD_MACOS)).toMatchObject({ kind: 'macos', macos: '26.1' });
  });
  it('a supported Mac has no stop state', () => {
    expect(stopReason(system({}))).toBeNull();
    expect(stopReason(null)).toBeNull();
  });
});

describe('the chip stop', () => {
  it('names the chip and offers the requirements, with no install or continue action', () => {
    render(<StopState reason={stopReason(M2)!} hosted={false} onCheck={() => undefined} />);
    expect(screen.getByRole('heading', { name: 'This Mac can’t run Splash.' })).toBeTruthy();
    expect(screen.getByText('Splash needs an Apple M3 or newer. This Mac has Apple M2 Pro.')).toBeTruthy();
    const link = screen.getByTestId('stop-requirements');
    expect(link.getAttribute('href')).toBe(REQUIREMENTS_URL);
    expect(link.getAttribute('target')).toBe('_blank');
    expect(screen.queryByRole('button', { name: /Install Homebrew|Continue/ })).toBeNull();
    expect(screen.queryByTestId('stop-check')).toBeNull();
  });

  it('falls back to the generic chip name when the chip is unknown', () => {
    render(<StopState reason={{ kind: 'chip', chip: null, macos: '27.0', reasons: [] }} hosted={false} onCheck={() => undefined} />);
    expect(screen.getByText('Splash needs an Apple M3 or newer. This Mac has Apple silicon.')).toBeTruthy();
  });

  it('in the app window the requirements open through the bridge, not a new tab', () => {
    const postMessage = vi.fn();
    Object.assign(globalThis, { webkit: { messageHandlers: { splashGUI: { postMessage } } } });
    try {
      render(<StopState reason={stopReason(M2)!} hosted onCheck={() => undefined} />);
      const link = screen.getByTestId('stop-requirements');
      expect(link.getAttribute('target')).toBeNull();
      expect(fireEvent.click(link)).toBe(false);
      expect(postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'openURL', url: REQUIREMENTS_URL }));
    } finally {
      delete (globalThis as { webkit?: unknown }).webkit;
    }
  });
});

describe('the macOS stop', () => {
  it('says what runs and offers Software Update and Check again', () => {
    const onCheck = vi.fn();
    render(<StopState reason={stopReason(OLD_MACOS)!} hosted={false} onCheck={onCheck} />);
    expect(screen.getByRole('heading', { name: 'macOS is too old.' })).toBeTruthy();
    expect(screen.getByText('Splash needs macOS 26.4 or later. This Mac runs 26.1.')).toBeTruthy();
    const update = screen.getByRole('link', { name: 'Open Software Update' });
    expect(update.getAttribute('href')).toBe(SOFTWARE_UPDATE_URL);
    expect(update.getAttribute('data-variant')).toBe('accent');
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    expect(onCheck).toHaveBeenCalledTimes(1);
  });

  it('in the app window Software Update goes through the bridge, which accepts System Settings URLs', () => {
    const postMessage = vi.fn();
    Object.assign(globalThis, { webkit: { messageHandlers: { splashGUI: { postMessage } } } });
    try {
      render(<StopState reason={stopReason(OLD_MACOS)!} hosted onCheck={() => undefined} />);
      expect(fireEvent.click(screen.getByTestId('stop-software-update'))).toBe(false);
      expect(postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'openURL', url: SOFTWARE_UPDATE_URL }));
    } finally {
      delete (globalThis as { webkit?: unknown }).webkit;
    }
  });

  it('in a browser the Software Update link is left to the browser', () => {
    render(<StopState reason={stopReason(OLD_MACOS)!} hosted={false} onCheck={() => undefined} />);
    // The probe records whether the page prevented the default, then stops jsdom from navigating.
    let prevented: boolean | null = null;
    const probe = (event: Event) => {
      prevented = event.defaultPrevented;
      event.preventDefault();
    };
    document.addEventListener('click', probe);
    try {
      fireEvent.click(screen.getByTestId('stop-software-update'));
    } finally {
      document.removeEventListener('click', probe);
    }
    expect(prevented).toBe(false);
  });
});
