import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { useState } from 'preact/hooks';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ConfirmSheet } from '../src/components/ConfirmSheet';
import { JsonView } from '../src/components/CodeBlock';
import { SearchInput, SegmentedControl } from '../src/components/controls';
import { KeyValue } from '../src/components/KeyValue';
import { LogPane } from '../src/components/LogPane';
import { Menu } from '../src/components/Menu';
import { dismissToast, toast, toastError, ToastHost, toastQueue } from '../src/components/Toast';
import { useApi } from '../src/lib/use-api';
import { GOTO, useShortcuts } from '../src/lib/shortcuts';
import { downloadProgress, downloads, engine, handleEvent, onEvent, alerts } from '../src/store';

beforeEach(() => {
  toastQueue.value = [];
  downloads.value = [];
  alerts.value = [];
  engine.value = null;
});

describe('Toast', () => {
  it('shows one toast at a time and drains the queue', async () => {
    render(<ToastHost />);
    toast('Settings saved');
    toast('Copied');
    expect(await screen.findByText('Settings saved')).toBeTruthy();
    expect(screen.queryByText('Copied')).toBeNull();
    dismissToast();
    expect(await screen.findByText('Copied')).toBeTruthy();
  });

  it('error toasts carry the API detail and use role=alert', async () => {
    render(<ToastHost />);
    toastError('Couldn’t stop the engine.', { status: 409, code: 'model_switch_busy', message: 'busy' });
    const el = await screen.findByRole('alert');
    expect(el.textContent).toContain('409 model_switch_busy · busy');
  });

  it('auto-hides info toasts after 6 s', async () => {
    vi.useFakeTimers();
    render(<ToastHost />);
    toast('Download queued');
    await vi.advanceTimersByTimeAsync(10);
    expect(screen.getByText('Download queued')).toBeTruthy();
    await vi.advanceTimersByTimeAsync(6100);
    expect(toastQueue.value).toHaveLength(0);
  });
});

describe('ConfirmSheet', () => {
  it('needs the typed word (case-insensitive) before confirming', () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmSheet open title="Delete 2 models." confirmLabel="Delete · 31.5 GB" typedWord="DELETE" onConfirm={onConfirm} onClose={() => undefined}>
        <p>This removes two models.</p>
      </ConfirmSheet>,
    );
    expect(screen.getByRole('alertdialog')).toBeTruthy();
    const confirm = screen.getByTestId('confirm-sheet-confirm') as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.input(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'delete' } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('uses the accent button only when important', () => {
    render(
      <ConfirmSheet open important title="Stop." confirmLabel="Stop" onConfirm={() => undefined} onClose={() => undefined}>
        x
      </ConfirmSheet>,
    );
    expect(screen.getByTestId('confirm-sheet-confirm').getAttribute('data-variant')).toBe('accent');
  });
});

describe('SegmentedControl', () => {
  function Harness() {
    const [v, setV] = useState<'5' | '15' | '60'>('5');
    return (
      <>
        <SegmentedControl
          label="Window"
          value={v}
          onChange={setV}
          options={[
            { value: '5', label: '5 m' },
            { value: '15', label: '15 m' },
            { value: '60', label: '60 m' },
          ]}
        />
        <output>{v}</output>
      </>
    );
  }
  it('is a radiogroup moved by arrow keys', () => {
    render(<Harness />);
    const group = screen.getByRole('radiogroup', { name: 'Window' });
    fireEvent.keyDown(group, { key: 'ArrowRight' });
    expect(screen.getByText('15', { selector: 'output' })).toBeTruthy();
    fireEvent.keyDown(group, { key: 'ArrowLeft' });
    fireEvent.keyDown(group, { key: 'ArrowLeft' });
    expect(screen.getByText('60', { selector: 'output' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: '60 m' }).getAttribute('aria-checked')).toBe('true');
  });
});

describe('Menu', () => {
  it('opens, moves with arrows, selects with click and closes on Escape', () => {
    const pick = vi.fn();
    render(
      <Menu
        label="Switch model"
        radio
        items={[
          { key: 'a', label: 'a/one', checked: true, onSelect: () => pick('a') },
          { key: 'b', label: 'b/two', onSelect: () => pick('b') },
          { key: 'c', label: 'c/three', disabled: true, detail: 'Not installed' },
        ]}
      />,
    );
    const trigger = screen.getByRole('button', { name: /Switch model/ });
    fireEvent.click(trigger);
    const menu = screen.getByRole('menu');
    expect(screen.getAllByRole('menuitemradio')).toHaveLength(3);
    fireEvent.keyDown(menu, { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByText('c/three'));
    expect(pick).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('b/two'));
    expect(pick).toHaveBeenCalledWith('b');
    expect(screen.queryByRole('menu')).toBeNull();
  });
});

describe('SearchInput', () => {
  it('clears on Escape and marks the primary search', () => {
    function H() {
      const [q, setQ] = useState('qwen');
      return <SearchInput label="Search models" value={q} onChange={setQ} primary />;
    }
    render(<H />);
    const box = screen.getByRole('searchbox', { name: 'Search models' }) as HTMLInputElement;
    expect(box.getAttribute('data-primary-search')).toBe('true');
    fireEvent.keyDown(box, { key: 'Escape' });
    expect(box.value).toBe('');
  });
});

describe('content components', () => {
  it('KeyValue shows a dash for missing values', () => {
    render(<KeyValue items={[{ key: 'a', label: 'Limit', value: null }]} />);
    expect(screen.getByText('—')).toBeTruthy();
  });

  it('LogPane highlights matches and marks levels', () => {
    render(<LogPane label="Engine log" lines={[{ key: 1, text: 'Weights loaded in 2.1 s', level: 'info' }, { key: 2, text: 'Traceback boom', level: 'error' }]} highlight="loaded" />);
    expect(screen.getByRole('log', { name: 'Engine log' })).toBeTruthy();
    expect(screen.getByText('loaded').tagName).toBe('MARK');
    expect(document.querySelector('[data-level="error"]')).toBeTruthy();
  });

  it('JsonView renders nested groups', () => {
    render(<JsonView value={{ scheduler: { decoding: 1 }, ok: true }} />);
    expect(screen.getByText('"decoding":')).toBeTruthy();
  });
});

describe('store event bus', () => {
  it('hello seeds engine, alerts and downloads; listeners get every event', () => {
    const seen: string[] = [];
    const off = onEvent('models.changed', (d) => seen.push((d as { reason: string }).reason));
    handleEvent('hello', {
      engine: { state: 'ready', model: 'a/b' },
      alerts: [{ id: 'x', severity: 'warn', message: 'm' }],
      downloads: [{ id: 'd1', model: 'a/b', state: 'running', bytes_total: 100, bytes_done: 25, created_at: '2026-10-03T00:00:00Z' }],
    });
    expect(engine.value?.state).toBe('ready');
    expect(alerts.value).toHaveLength(1);
    expect(downloadProgress.value).toBe(0.25);
    handleEvent('download.progress', { id: 'd1', model: 'a/b', state: 'running', bytes_total: 100, bytes_done: 75, created_at: '2026-10-03T00:00:00Z' });
    expect(downloadProgress.value).toBe(0.75);
    handleEvent('download.state', { id: 'd1', model: 'a/b', state: 'done', bytes_total: 100, bytes_done: 100, created_at: '2026-10-03T00:00:00Z' });
    expect(downloadProgress.value).toBeNull();
    handleEvent('models.changed', { reason: 'downloaded', model: 'a/b' });
    off();
    handleEvent('models.changed', { reason: 'deleted', model: 'a/b' });
    expect(seen).toEqual(['downloaded']);
  });

  it('engine.state and alert.cleared use the api.md names', () => {
    handleEvent('engine.state', { state: 'starting', phase: 'installing', model: 'a/b' });
    expect(engine.value?.state).toBe('starting.installing');
    handleEvent('alert', { id: 'q', severity: 'warn', message: 'Queue full' });
    handleEvent('alert.cleared', { id: 'q' });
    expect(alerts.value).toHaveLength(0);
  });
});

describe('useApi', () => {
  it('loads, exposes errors and reloads', async () => {
    let n = 0;
    const fetcher = vi.fn(async () => {
      n += 1;
      if (n === 1) throw new Error('boom');
      return n;
    });
    let api: ReturnType<typeof useApi<number>> | null = null;
    function H() {
      api = useApi(fetcher);
      return <p>{api.error ? 'error' : String(api.data)}</p>;
    }
    render(<H />);
    expect(await screen.findByText('error')).toBeTruthy();
    await api!.reload();
    await waitFor(() => expect(screen.getByText('2')).toBeTruthy());
  });
});

describe('shortcuts', () => {
  it('g chords navigate, ignored while typing', () => {
    const navigate = vi.fn();
    function H() {
      useShortcuts({ navigate, toggleTheme: () => undefined, openHelp: () => undefined });
      return <input aria-label="field" />;
    }
    render(<H />);
    fireEvent.keyDown(document.body, { key: 'g' });
    fireEvent.keyDown(document.body, { key: 'm' });
    expect(navigate).toHaveBeenCalledWith(GOTO.m);
    const field = screen.getByLabelText('field');
    fireEvent.keyDown(field, { key: 'g' });
    fireEvent.keyDown(field, { key: 's' });
    expect(navigate).toHaveBeenCalledTimes(1);
  });
});
