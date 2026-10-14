/** Settings envelope fallbacks (settings.json version 3). */
import { describe, expect, it } from 'vitest';
import { readEnvelope, SETTINGS_VERSION } from '../src/routes/settings/api';

describe('readEnvelope', () => {
  it('falls back to a version-3 document, the version the manager accepts on PUT', () => {
    expect(SETTINGS_VERSION).toBe(3);
    expect(readEnvelope({}).settings).toEqual({ version: 3, global: {}, models: {} });
    expect(readEnvelope({ settings: { global: { ui: {} } } }).settings.version).toBe(3);
  });

  it('keeps the version the manager sent', () => {
    expect(readEnvelope({ settings: { version: 4, global: {}, models: {} } }).settings.version).toBe(4);
  });
});
