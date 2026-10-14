/**
 * What Splash 1.3.2 changes in Settings and Chat: the SSD cache's Off / Auto / Size control and
 * its new defaults, the new engine options and their checks, advanced fields folded at the end of
 * a section, the thinking budget for Chat and profiles, Messages' 1024-token thinking budget, and
 * a reply's own speculative decoding numbers.
 */
import { signal } from '@preact/signals';
import { fireEvent, render, screen } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import type { SchemaField } from '../src/api/models';
import { StreamAccumulator } from '../src/routes/chat/accumulator';
import { AdvancedFields } from '../src/routes/settings/AdvancedFields';
import { splitAdvanced } from '../src/routes/settings/form';
import { overlayErrors } from '../src/routes/settings/OverlayEditor';
import { cacheDiskMode, SettingField } from '../src/routes/settings/SettingField';
import type { SettingsForm } from '../src/routes/settings/state';
import { fieldError, overlayFieldError } from '../src/routes/settings/validate';
import { templatesFor } from '../src/routes/tools/endpoints';
import { formatSettingValue, sameValue } from '../src/routes/welcome/logic';

function field(over: Partial<SchemaField>): SchemaField {
  return {
    key: 'serve.max_cache_disk',
    label: 'SSD cache',
    help: '',
    section: 'cache',
    control: 'size',
    applies: 'restart',
    scope: 'G',
    advanced: false,
    flag: '--max-cache-disk',
    default: 'auto',
    ...over,
  } as SchemaField;
}

/** The parts of a SettingsForm a SettingField reads, over one value. */
function fakeForm(initial: unknown): { form: SettingsForm; current: () => unknown } {
  let value = initial;
  const form = {
    model: null,
    envelope: signal({ read_only: false }),
    effective: signal(null),
    value: () => value,
    set: (_ref: unknown, v: unknown) => {
      value = v;
    },
    isDirty: () => false,
    errorsFor: () => [],
    warningsFor: () => [],
  } as unknown as SettingsForm;
  return { form, current: () => value };
}

describe('SSD cache: Off / Auto / Size', () => {
  it('reads each stored value as one of the three', () => {
    expect([cacheDiskMode('0'), cacheDiskMode(' 0 '), cacheDiskMode('auto'), cacheDiskMode('AUTO'), cacheDiskMode(undefined), cacheDiskMode('16G')]).toEqual([
      'off',
      'off',
      'auto',
      'auto',
      'auto',
      'size',
    ]);
  });

  it('switches between them, with 16G as the first size', () => {
    const { form, current } = fakeForm('auto');
    const { rerender } = render(<SettingField field={field({})} form={form} />);
    expect(screen.getByRole('radio', { name: 'Auto' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByText(/sizes the cache from the disk's free space/)).toBeTruthy();
    fireEvent.click(screen.getByRole('radio', { name: 'Size' }));
    expect(current()).toBe('16G');
    rerender(<SettingField field={field({})} form={form} />);
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('16G');
    fireEvent.click(screen.getByRole('radio', { name: 'Off' }));
    expect(current()).toBe('0');
  });
});

describe('Splash 1.3.2 engine options', () => {
  it('checks the SSD cache, its write limit, the thinking budget and the thinking end', () => {
    expect(fieldError({ key: 'serve.max_cache_disk', control: 'size' }, 'auto')).toBeNull();
    expect(fieldError({ key: 'serve.max_cache_disk_writes', control: 'size' }, '200G')).toBeNull();
    expect(fieldError({ key: 'serve.max_cache_disk_writes', control: 'size' }, 'auto')).toBe('use 0 for no limit, or a size such as 200G');
    const budget = { key: 'serve.thinking_token_budget', control: 'number' };
    expect([fieldError(budget, null), fieldError(budget, 0), fieldError(budget, 4096)]).toEqual([null, null, null]);
    expect(fieldError(budget, -1)).toBe('must be a nonnegative integer');
    const end = { key: 'serve.thinking_end_text', control: 'text' };
    expect(fieldError(end, null)).toBeNull();
    expect(fieldError(end, 'Time to answer.\n</think>')).toBeNull();
    expect(fieldError(end, 'Time to answer.')).toBe('--thinking-end-text must end with </think> and hold no other special token');
  });

  it('shows the write limit at 0 as no limit, and an unset thinking budget as no cap', () => {
    const writes = fakeForm('0');
    render(<SettingField field={field({ key: 'serve.max_cache_disk_writes', label: 'SSD cache writes a day', flag: '--max-cache-disk-writes', default: '0' })} form={writes.form} />);
    expect(screen.getByText('No limit')).toBeTruthy();
    const budget = fakeForm(null);
    render(<SettingField field={field({ key: 'serve.thinking_token_budget', label: 'Thinking budget', control: 'number', flag: '--thinking-token-budget', default: null })} form={budget.form} />);
    expect(screen.getByPlaceholderText('No cap')).toBeTruthy();
  });

  it('folds advanced fields at the end of a section, but not on the Advanced page', () => {
    const fields = [field({ key: 'a' }), field({ key: 'b', advanced: true }), field({ key: 'c' })];
    const cache = splitAdvanced(fields, 'cache');
    expect(cache.main.map((f) => f.key)).toEqual(['a', 'c']);
    expect(cache.advanced.map((f) => f.key)).toEqual(['b']);
    expect(splitAdvanced(fields, 'advanced').advanced).toEqual([]);
  });

  it('starts the fold closed again in the next section, since each section keys its own', () => {
    const fold = (slug: string) => (
      <AdvancedFields key={slug} keys={['b']} reveal={false}>
        <p>{slug}</p>
      </AdvancedFields>
    );
    const { rerender } = render(fold('cache'));
    const details = () => screen.getByTestId('settings-advanced') as HTMLDetailsElement;
    expect(details().open).toBe(false);
    details().open = true;
    fireEvent(details(), new Event('toggle'));
    rerender(fold('cache'));
    expect(details().open).toBe(true);
    rerender(fold('memory'));
    expect(details().open).toBe(false);
  });
});

describe('welcome preset diff', () => {
  it('shows and compares the automatic SSD cache', () => {
    expect(formatSettingValue('serve.max_cache_disk', 'auto')).toBe('SSD cache auto');
    expect(formatSettingValue('serve.max_cache_disk', '0')).toBe('SSD cache off');
    expect(sameValue('serve.max_cache_disk', 'auto', 'AUTO')).toBe(true);
    expect(sameValue('serve.max_cache_disk', 'auto', '0')).toBe(false);
  });
});

describe('profiles and sampling defaults', () => {
  it('take a thinking budget (-1 no cap) and any 64-bit seed', () => {
    expect(overlayFieldError('thinking_token_budget', 'Thinking budget', -1)).toBeNull();
    expect(overlayFieldError('thinking_token_budget', 'Thinking budget', 2048)).toBeNull();
    expect(overlayFieldError('thinking_token_budget', 'Thinking budget', -2)).toBe('Thinking budget must be −1 (no cap) or a whole number of at least 0.');
    expect(overlayFieldError('seed', 'Seed', -1)).toBeNull();
    expect(overlayErrors({ seed: 1.5 }).seed).toContain('Seed must be a whole number');
  });

  it('refuse enabled Messages thinking under 1024 budget tokens, as Splash does', () => {
    const refusal = 'thinking.budget_tokens must be an integer of at least 1024';
    expect(overlayFieldError('thinking', 'thinking', { type: 'enabled' })).toBe(refusal);
    expect(overlayFieldError('thinking', 'thinking', { type: 'enabled', budget_tokens: 512 })).toBe(refusal);
    expect(overlayFieldError('thinking', 'thinking', { type: 'enabled', budget_tokens: 1024 })).toBeNull();
    expect(overlayFieldError('thinking', 'thinking', { type: 'adaptive' })).toBeNull();
  });

  it('the Messages thinking template sends a budget Splash accepts', () => {
    const template = templatesFor('messages').find((x) => x.id === 'thinking')!;
    const body = template.build('m') as { max_tokens: number; thinking: { budget_tokens: number } };
    expect(body.thinking.budget_tokens).toBe(1024);
    expect(body.max_tokens).toBe(2048);
  });
});

describe('a reply’s speculative decoding', () => {
  it('keeps the usage chunk’s metrics.speculation next to the timings', () => {
    const acc = new StreamAccumulator(0);
    acc.push({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], timings: { predicted_n: 50, draft_n: 40, draft_n_accepted: 30 } });
    acc.push({ choices: [], usage: { completion_tokens: 50 }, metrics: { speculation: { drafted_tokens: 40, accepted_draft_tokens: 30, cycles: 12 } } });
    expect(acc.snapshot().timings?.draft_n_accepted).toBe(30);
    expect(acc.snapshot().speculation).toEqual({ drafted_tokens: 40, accepted_draft_tokens: 30, cycles: 12 });
  });
});
