import { render, within } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryBand, NeuralEngineBand, idleText } from '../src/routes/status/Bands';

// Shapes from splash/runtime/engine/Status.cpp at 1.3.0 (appendAneFfn, appendWeights).
const SPLIT = {
  ane_ffn: {
    state: 'split',
    share: 0.25,
    minimum_rows: 1024,
    reason: 'split',
    split_commands: 40,
    reruns: 0,
    ane_ms: 1240,
    evaluations: 1234,
  },
};

const MOE_OFF = {
  ane_ffn: {
    state: 'off',
    share: 0,
    minimum_rows: 0,
    reason: 'the target has no dense FFN layers',
    split_commands: 0,
    reruns: 0,
    ane_ms: 0,
    evaluations: 0,
  },
};

/** The band's section, found by its label (the stopped and not-reported forms have no id). */
function band(name = 'Neural Engine'): HTMLElement {
  const found = [...document.querySelectorAll('section')].find((s) => s.querySelector('h2')?.textContent === name);
  if (!found) throw new Error(`no ${name} band`);
  return found;
}

function row(label: string): HTMLElement {
  const dt = within(band()).getByText(label, { selector: 'dt' });
  return dt.nextElementSibling as HTMLElement;
}

beforeEach(() => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }));
});

describe('Status → Neural Engine band', () => {
  it('shows a live split in the accent with its share, chunk, evaluations, time and reruns', () => {
    render(<NeuralEngineBand raw={SPLIT} stopped={false} />);
    const state = row('Prefill split');
    expect(state.textContent).toBe('Split');
    expect(state.querySelector('[data-accent="true"]')?.textContent).toBe('Split');
    expect(row('Share').textContent).toBe('25% of FFN channels');
    expect(row('Minimum prompt chunk').textContent).toBe('1,024 tokens');
    expect(row('Evaluations').textContent).toBe('1,234in 40 prefill commands');
    expect(row('Neural Engine time').textContent).toBe('1.24 s');
    expect(row('Reruns').textContent).toBe('0chunks the GPU ran again alone');
    expect(band().querySelectorAll('[data-accent="true"]')).toHaveLength(1);
  });

  it('shows the MoE "off" case as GPU only with Splash’s reason and no counters', () => {
    render(<NeuralEngineBand raw={MOE_OFF} stopped={false} />);
    expect(row('Prefill split').textContent).toBe('Off · GPU onlythe target has no dense FFN layers');
    expect(within(band()).queryByText('Share')).toBeNull();
    expect(within(band()).queryByText('Evaluations')).toBeNull();
    expect(band().querySelectorAll('[data-accent="true"]')).toHaveLength(0);
  });

  it('names the setting when --disable-ane turned it off', () => {
    render(<NeuralEngineBand raw={{ ane_ffn: { ...MOE_OFF.ane_ffn, reason: 'as given' } }} stopped={false} />);
    expect(row('Prefill split').textContent).toBe('Off · GPU onlyTurned off by GPU-only prefill in Settings (--disable-ane).');
  });

  it('shows a stopped split without the accent, with why it stopped and what it ran', () => {
    const raw = { ane_ffn: { ...SPLIT.ane_ffn, state: 'stopped', reason: 'a Neural Engine evaluation timed out', reruns: 1 } };
    render(<NeuralEngineBand raw={raw} stopped={false} />);
    expect(row('Prefill split').textContent).toBe('Stopped · GPU only until the engine restartsa Neural Engine evaluation timed out');
    expect(row('Reruns').textContent).toBe('1chunks the GPU ran again alone');
    expect(band().querySelectorAll('[data-accent="true"]')).toHaveLength(0);
  });

  it('says an engine older than 1.3.0 does not report it', () => {
    render(<NeuralEngineBand raw={{ schema_version: 6, kv: {} }} stopped={false} />);
    expect(band().textContent).toContain('Not reported by this Splash version.');
  });

  it('collapses while the engine is stopped', () => {
    render(<NeuralEngineBand raw={null} stopped />);
    expect(band().textContent).toContain('Engine stopped.');
  });
});

describe('Status → Memory band, Weights row', () => {
  function weights(): string {
    const dt = within(band('Memory')).getByText('Weights', { selector: 'dt' });
    return (dt.nextElementSibling as HTMLElement).textContent ?? '';
  }

  it('reports weights in memory, the idle release and the restores', () => {
    render(<MemoryBand raw={{ weights: { idle_release_seconds: 600, released: false, restores: 2 } }} stopped={false} />);
    expect(weights()).toBe('in memoryreleased after 10 m idle · 2 restores');
  });

  it('reports released weights and idle release off', () => {
    render(<MemoryBand raw={{ weights: { idle_release_seconds: null, released: true, restores: 1 } }} stopped={false} />);
    expect(weights()).toBe('released · reload on the next requestidle release off · 1 restore');
  });

  it('shows a dash without the weights block (Splash 1.2.0)', () => {
    render(<MemoryBand raw={{ memory_governor: {} }} stopped={false} />);
    expect(weights()).toBe('—');
  });

  it('writes the idle release as Splash spells durations', () => {
    expect([idleText(600), idleText(7200), idleText(90), idleText(5400)]).toEqual(['10 m', '2 h', '90 s', '90 m']);
  });
});
