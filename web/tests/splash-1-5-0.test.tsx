/**
 * What Splash 1.5.0 adds on screen: Chat's reasoning effort select says what each effort
 * does to the served model, from the engine's `/v1/models` `reasoning`.
 */
import { render, screen } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import { effortOptions } from '../src/routes/chat/logic';
import { t } from '../src/strings/chat';

const labels = (reasoning: Parameters<typeof effortOptions>[0]) => effortOptions(reasoning).map((o) => o.label);

describe('Chat reasoning effort', () => {
  it('lists the plain efforts while the engine has not said', () => {
    expect(labels(null)).toEqual(['Model default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
    expect(effortOptions(null).map((o) => o.value)).toEqual(['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
  });

  it('names what Qwen3.8’s template does with each effort', () => {
    expect(labels({ thinks: true, efforts: ['low', 'medium', 'xhigh'], default: 'xhigh' })).toEqual([
      'Model default · xhigh',
      'none',
      'minimal · same as low',
      'low',
      'medium',
      'high · same as xhigh',
      'xhigh',
      'max · same as xhigh',
    ]);
  });

  it('says a switch-only template reasons alike at every effort', () => {
    expect(labels({ thinks: true, efforts: [], default: null })).toEqual([
      'Model default · reasoning on',
      'none',
      'minimal · reasoning on',
      'low · reasoning on',
      'medium · reasoning on',
      'high · reasoning on',
      'xhigh · reasoning on',
      'max · reasoning on',
    ]);
    expect(labels({ thinks: false, efforts: [], default: null })[0]).toBe('Model default · reasoning off');
  });

  it('leaves a model that decides itself (Flint) plain', () => {
    expect(labels({ thinks: null, efforts: [], default: null })).toEqual(labels(null));
  });

  it('explains the template in the help line', () => {
    render(<p>{t('chat.help.reasoning_efforts', { efforts: 'low, medium, xhigh' })}</p>);
    expect(screen.getByText(/tells low, medium, xhigh apart/)).toBeTruthy();
  });
});
