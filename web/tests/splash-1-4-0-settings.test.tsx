/**
 * What Splash 1.4.0 changes in Settings: contexts past the native window (YaRN) on the slider and
 * in the text box, the streamed stores' sizes with their off, and Draft verification's choices.
 */
import { signal } from '@preact/signals';
import { fireEvent, render, screen } from '@testing-library/preact';
import { describe, expect, it } from 'vitest';
import type { SchemaField } from '../src/api/models';
import { choiceLabel } from '../src/routes/settings/labels';
import { contextFromSlider, contextTickLabel, contextToSlider, SettingField } from '../src/routes/settings/SettingField';
import type { SettingsForm } from '../src/routes/settings/state';
import { fieldError } from '../src/routes/settings/validate';
import { sameValue } from '../src/routes/welcome/logic';

function field(over: Partial<SchemaField>): SchemaField {
  return {
    key: 'serve.max_context',
    label: 'Context limit',
    help: '',
    section: 'memory_context',
    control: 'context',
    applies: 'restart',
    scope: 'GM',
    advanced: false,
    flag: '--max-context',
    default: 'auto',
    ...over,
  } as SchemaField;
}

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

describe('context past the native window', () => {
  it('reaches 1M at the slider’s top step and whole K below it', () => {
    expect(contextFromSlider(20)).toBe('1M');
    expect(contextFromSlider(19)).toBe('512K');
    expect(contextFromSlider(18)).toBe('256K');
    expect(contextFromSlider(12)).toBe('4K');
    expect(contextToSlider(1000000)).toBe(20);
    expect(contextToSlider(1010000)).toBe(20);
    expect(contextToSlider(262144)).toBe(18);
    expect(contextToSlider(1024)).toBe(12);
    expect([contextTickLabel(524288), contextTickLabel(1000000)]).toEqual(['512K', '1M']);
  });

  it('accepts K and M up to 1,010,000 and notes YaRN past 256K', () => {
    const rule = { key: 'serve.max_context', control: 'context' };
    expect(fieldError(rule, '1M')).toBeNull();
    expect(fieldError(rule, '1010000')).toBeNull();
    expect(fieldError(rule, '2M')).toBe(
      "must be 'auto' or a token count up to 1010000, such as 100K or 1M (K = 1024 tokens, M = 1000000 tokens)",
    );
    const { form } = fakeForm('1M');
    render(<SettingField field={field({})} form={form} />);
    expect(screen.getByText(/1,000,000 tokens/)).toBeTruthy();
    expect(screen.getByText(/Splash turns on YaRN/)).toBeTruthy();
    expect(screen.getByText('512K')).toBeTruthy();
    expect(screen.getByText('1M')).toBeTruthy();
  });

  it('shows no YaRN note at the native window', () => {
    const { form } = fakeForm('256K');
    render(<SettingField field={field({})} form={form} />);
    expect(screen.queryByText(/Splash turns on YaRN/)).toBeNull();
  });

  it('writes 1M from the slider’s top', () => {
    const { form, current } = fakeForm('128K');
    render(<SettingField field={field({})} form={form} />);
    fireEvent.input(screen.getByRole('slider'), { target: { value: '20' } });
    expect(current()).toBe('1M');
  });

  it('compares 1M with its token count in the welcome diff', () => {
    expect(sameValue('serve.max_context', '1M', '1000000')).toBe(true);
    expect(sameValue('serve.max_context', '1M', '1024K')).toBe(false);
  });
});

describe('the streamed stores', () => {
  const store = (key: string, flag: string) =>
    field({ key, label: key, control: 'size', section: key === 'serve.sparse_kv_memory' ? 'cache' : 'memory_context', flag, advanced: true });

  it('check auto, off and sizes as Splash does', () => {
    for (const key of ['serve.expert_memory', 'serve.ple_memory', 'serve.sparse_kv_memory']) {
      const rule = { key, control: 'size' };
      expect(fieldError(rule, 'auto')).toBeNull();
      expect(fieldError(rule, null)).toBeNull();
      expect(fieldError(rule, '2.5G')).toBeNull();
    }
    expect(fieldError({ key: 'serve.expert_memory', control: 'size' }, 'off')).toBeNull();
    expect(fieldError({ key: 'serve.sparse_kv_memory', control: 'size' }, 'off')).toBe('use auto, or a size such as 1200M');
    expect(fieldError({ key: 'serve.ple_memory', control: 'size' }, '0')).toBe('use auto, off, or a size such as 2G');
  });

  it('show auto, off and the parsed size', () => {
    const auto = fakeForm('auto');
    const { unmount } = render(<SettingField field={store('serve.expert_memory', '--expert-memory')} form={auto.form} />);
    expect(screen.getByText('Automatic')).toBeTruthy();
    unmount();
    const off = fakeForm('off');
    render(<SettingField field={store('serve.ple_memory', '--ple-memory')} form={off.form} />);
    expect(screen.getByText('Off · the whole table loads')).toBeTruthy();
    fireEvent.input(screen.getByRole('textbox'), { target: { value: '16G' } });
    expect(off.current()).toBe('16G');
  });
});

describe('Draft verification', () => {
  it('names what each choice does', () => {
    expect(choiceLabel('serve.verify_rows', 'all')).toBe('All rows · reproducible MoE decoding');
    expect(choiceLabel('serve.verify_rows', 'auto')).toBe('Auto · faster, MoE output can vary');
    expect(choiceLabel('serve.kv_format', 'int8')).toBe('int8');
  });
});
