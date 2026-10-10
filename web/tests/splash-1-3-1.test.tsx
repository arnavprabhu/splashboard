/**
 * What Splash 1.3.1 changed on screen: an installed Splash package is listed as no longer
 * loading (Delete only), and the Status numbers band names a hot Mac's thermal state.
 */
import { render, screen } from '@testing-library/preact';
import { beforeEach, describe, expect, it } from 'vitest';
import type { InstalledModel } from '../src/api/models';
import { ManagerRow } from '../src/routes/models/ManagerRow';
import { thermalState } from '../src/routes/status/Numbers';
import { engine } from '../src/store';

beforeEach(() => {
  engine.value = { state: 'stopped', model: null } as never;
});

describe('an installed Splash package', () => {
  const id = 'incoai/Qwen3.8-27B-Splash';
  const notice = 'Splash no longer loads Splash packages. Delete this one and download mlx-community/Qwen3.8-27B-4bit, which loads the same weights.';
  const model = {
    id,
    repo_id: id,
    format: 'legacy',
    family: 'Qwen3.8-27B',
    language_only: false,
    size_bytes: 20e9,
    unique_bytes: 20e9,
    pinned: false,
    status: 'unsupported',
    notice,
  } as InstalledModel;
  const noop = () => undefined;

  it('says it no longer loads, explains why and offers Delete but not Load', () => {
    render(<ManagerRow id={id} model={model} index={1} active={false} onOpen={noop} onUnload={noop} onVerify={noop} onUpdate={noop} onDelete={noop} onCancel={noop} />);
    expect([...document.querySelectorAll('.tag')].some((el) => el.textContent === 'No longer loads')).toBe(true);
    expect(screen.getByTestId('model-notice').textContent).toBe(notice);
    expect(screen.queryAllByTestId('row-delete').length).toBe(1);
    expect(screen.queryByTestId('row-load')).toBeNull();
  });
});

describe('thermal state', () => {
  it('is shown only above nominal, from /status.thermal_state', () => {
    expect(thermalState({ thermal_state: 'nominal' })).toBeNull();
    expect(thermalState({})).toBeNull();
    expect(thermalState(null)).toBeNull();
    expect(thermalState({ thermal_state: 'fair' })).toBe('fair');
    expect(thermalState({ thermal_state: 'serious' })).toBe('serious');
    expect(thermalState({ thermal_state: 'critical' })).toBe('critical');
    expect(thermalState({ thermal_state: 'molten' })).toBeNull();
  });
});
