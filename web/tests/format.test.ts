import { describe, expect, it } from 'vitest';
import {
  DASH,
  formatBytes,
  formatBytesPerSecond,
  formatCompact,
  formatCount,
  formatDate,
  formatDuration,
  formatIndex,
  formatMs,
  formatPercent,
  formatRelativeTime,
  formatTokPerSec,
  formatTokens,
  tildePath,
} from '../src/lib/format';

describe('formatBytes', () => {
  it('uses binary units by default, matching Splash K/M/G', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1024)).toBe('1 KB');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(32 * 1024 ** 3)).toBe('32 GB');
    expect(formatBytes(128 * 1024 ** 2)).toBe('128 MB');
  });
  it('supports decimal units for Hub sizes', () => {
    expect(formatBytes(21.2e9, { base: 1000 })).toBe('21.2 GB');
    expect(formatBytes(999, { base: 1000 })).toBe('999 B');
  });
  it('respects digits and handles negatives and junk', () => {
    expect(formatBytes(1.234 * 1024 ** 3, { digits: 2 })).toBe('1.23 GB');
    expect(formatBytes(-2048)).toBe('-2 KB');
    expect(formatBytes(null)).toBe(DASH);
    expect(formatBytes(Number.NaN)).toBe(DASH);
    expect(formatBytes(Infinity)).toBe(DASH);
  });
  it('formats rates', () => {
    expect(formatBytesPerSecond(3 * 1024 ** 2)).toBe('3 MB/s');
    expect(formatBytesPerSecond(undefined)).toBe(DASH);
  });
});

describe('formatTokPerSec', () => {
  it('uses one decimal below 100 and whole numbers above', () => {
    expect(formatTokPerSec(42.345)).toBe('42.3 tok/s');
    expect(formatTokPerSec(0)).toBe('0.0 tok/s');
    expect(formatTokPerSec(1234.6)).toBe('1,235 tok/s');
    expect(formatTokPerSec(42.345, { unit: false })).toBe('42.3');
    expect(formatTokPerSec(null)).toBe(DASH);
  });
});

describe('counts', () => {
  it('formats counts with separators', () => {
    expect(formatCount(1234567)).toBe('1,234,567');
    expect(formatCount(undefined)).toBe(DASH);
  });
  it('compacts large numbers', () => {
    expect(formatCompact(999)).toBe('999');
    expect(formatCompact(1234)).toBe('1.2K');
    expect(formatCompact(3_400_000)).toBe('3.4M');
    expect(formatCompact(250_000)).toBe('250K');
    expect(formatCompact(-1500)).toBe('-1.5K');
  });
  it('formats context sizes and indexes', () => {
    expect(formatTokens(131072)).toBe('128K');
    expect(formatTokens(1000)).toBe('1,000');
    expect(formatIndex(1)).toBe('01');
    expect(formatIndex(12)).toBe('12');
  });
});

describe('formatPercent', () => {
  it('formats ratios', () => {
    expect(formatPercent(0.875)).toBe('87.5%');
    expect(formatPercent(1)).toBe('100%');
    expect(formatPercent(0)).toBe('0%');
    expect(formatPercent(0.12345, 2)).toBe('12.35%');
    expect(formatPercent(0.0001)).toBe('<0.1%');
    expect(formatPercent(null)).toBe(DASH);
  });
});

describe('durations', () => {
  it('formats milliseconds', () => {
    expect(formatMs(4.25)).toBe('4.3 ms');
    expect(formatMs(850)).toBe('850 ms');
    expect(formatMs(1240)).toBe('1.24 s');
    expect(formatMs(12_500)).toBe('12.5 s');
    expect(formatMs(125_000)).toBe('2 m 05 s');
    expect(formatMs(-1)).toBe(DASH);
  });
  it('formats seconds', () => {
    expect(formatDuration(45)).toBe('45 s');
    expect(formatDuration(200)).toBe('3 m 20 s');
    expect(formatDuration(2 * 3600 + 5 * 60)).toBe('2 h 05 m');
    expect(formatDuration(3 * 86400 + 4 * 3600)).toBe('3 d 4 h');
    expect(formatDuration(null)).toBe(DASH);
  });
});

describe('formatRelativeTime', () => {
  const now = new Date(2026, 9, 3, 12, 0, 0).getTime();
  it('covers past and future ranges', () => {
    expect(formatRelativeTime(now - 10_000, now)).toBe('just now');
    expect(formatRelativeTime(now - 3 * 60_000, now)).toBe('3 min ago');
    expect(formatRelativeTime(now + 3 * 60_000, now)).toBe('in 3 min');
    expect(formatRelativeTime(now - 2 * 3600_000, now)).toBe('2 h ago');
    expect(formatRelativeTime(now - 26 * 3600_000, now)).toBe('yesterday');
    expect(formatRelativeTime(now + 26 * 3600_000, now)).toBe('tomorrow');
    expect(formatRelativeTime(now - 5 * 86400_000, now)).toBe('5 d ago');
  });
  it('falls back to an absolute date after a week', () => {
    expect(formatRelativeTime(new Date(2026, 8, 1, 9), now)).toBe('2026-09-01');
    expect(formatDate(new Date(2026, 0, 5))).toBe('2026-01-05');
  });
  it('accepts ISO strings and rejects junk', () => {
    expect(formatRelativeTime(new Date(now - 120_000).toISOString(), now)).toBe('2 min ago');
    expect(formatRelativeTime('not a date', now)).toBe(DASH);
    expect(formatRelativeTime(null, now)).toBe(DASH);
  });
});

describe('tildePath', () => {
  it('shortens the home folder to ~ when it is known', () => {
    expect(tildePath('/Users/arnav/.splash/cache', '/Users/arnav')).toBe('~/.splash/cache');
    expect(tildePath('/Users/arnav', '/Users/arnav')).toBe('~');
    expect(tildePath('/home/me/x', '/home/me')).toBe('~/x');
    expect(tildePath('/Users/arnav/x', '/Users/arnav/')).toBe('~/x');
  });

  it('leaves other users\' folders alone, even under /Users', () => {
    expect(tildePath('/Users/Shared/x', '/Users/bob')).toBe('/Users/Shared/x');
    expect(tildePath('/Users/bob2/x', '/Users/bob')).toBe('/Users/bob2/x');
    expect(tildePath('/Users/bobby', '/Users/bob')).toBe('/Users/bobby');
  });

  it('leaves every other path alone', () => {
    expect(tildePath('/Volumes/Fast SSD/cache', '/Users/arnav')).toBe('/Volumes/Fast SSD/cache');
    expect(tildePath('~/.splash/cache', '/Users/arnav')).toBe('~/.splash/cache');
  });

  it('changes nothing while the home folder is unknown', () => {
    expect(tildePath('/Users/arnav/.splash/cache')).toBe('/Users/arnav/.splash/cache');
    expect(tildePath('/Users/arnav/.splash/cache', null)).toBe('/Users/arnav/.splash/cache');
  });
});
