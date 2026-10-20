import { describe, expect, it } from 'vitest';
import { minimumEngine } from '../src/routes/welcome/logic';
import { t } from '../src/strings/welcome';

describe('welcome: Splash too old', () => {
  it('names the minimum from the manager’s supported range', () => {
    expect(minimumEngine('>=1.4.0 <1.5.0')).toBe('1.4.0');
    expect(minimumEngine('>=1.2.0 <1.3.0')).toBe('1.2.0');
    expect(minimumEngine(undefined)).toBe('1.4.0');
    expect(t('welcome.engine.too_old', { version: '1.3.2', min: minimumEngine('>=1.4.0 <1.5.0') })).toBe(
      'Splash 1.3.2 is too old. Update to 1.4.0 or newer.',
    );
  });
});
