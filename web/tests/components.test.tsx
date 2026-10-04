import { fireEvent, render, screen } from '@testing-library/preact';
import type { ComponentChildren } from 'preact';
import { useState } from 'preact/hooks';
import { describe, expect, it, vi } from 'vitest';
import { Router } from 'wouter-preact';
import { memoryLocation } from 'wouter-preact/memory-location';
import {
  AlertBand,
  CopyButton,
  Field,
  ListRow,
  NavBand,
  ProgressBar,
  Sheet,
  SizeInput,
  StatusChip,
  StickySaveBar,
  SubNav,
  TagList,
  Toggle,
  TOOLS_TABS,
  isActive,
} from '../src/components';
import { setTheme } from '../src/store/theme';

function withRouter(ui: ComponentChildren, path = '/status') {
  const { hook } = memoryLocation({ path });
  return render(<Router hook={hook}>{ui}</Router>);
}

describe('StatusChip', () => {
  it('uses the accent only for live states', () => {
    const { container, rerender } = render(<StatusChip state="ready" />);
    expect(container.querySelector('.chip')?.getAttribute('data-live')).toBe('true');
    expect(screen.getByRole('status').textContent).toContain('Ready');
    rerender(<StatusChip state="starting.loading" />);
    expect(container.querySelector('.chip')?.getAttribute('data-live')).toBe('false');
    expect(screen.getByRole('status').textContent).toContain('Loading');
  });
});

describe('NavBand', () => {
  it('renders the wordmark, meta, links and marks the active link', () => {
    setTheme('light');
    withRouter(<NavBand engineVersion="1.2.0" model="mlx-community/Qwen3.8-27B-4bit" state="busy" />, '/tools/tokenizer');
    expect(screen.getByText('Splash GUI')).toBeTruthy();
    expect(screen.getByText('Splash 1.2.0')).toBeTruthy();
    expect(screen.getByText('Generating', { exact: false })).toBeTruthy();
    const links = screen.getAllByRole('link').map((a) => a.textContent);
    expect(links).toEqual(['Splash GUI', 'Status', 'Models', 'Chat', 'Tools', 'Integrations', 'Logs', 'Settings']);
    expect(screen.getByText('Tools').getAttribute('aria-current')).toBe('page');
    expect(screen.getByText('Status').getAttribute('aria-current')).toBeNull();
    expect(screen.queryByText('Log out')).toBeNull();
  });

  it('shows Log out when auth is on and toggles the theme', () => {
    setTheme('light');
    const onLogout = vi.fn();
    withRouter(<NavBand authEnabled onLogout={onLogout} />);
    fireEvent.click(screen.getByText('Log out'));
    expect(onLogout).toHaveBeenCalled();
    const toggle = screen.getByTestId('theme-toggle');
    expect(toggle.textContent).toBe('☾ Dark');
    fireEvent.click(toggle);
    expect(toggle.textContent).toBe('☀ Light');
    setTheme('light');
  });

  it('matches nested paths as active', () => {
    expect(isActive('/models/downloader', { href: '/models' })).toBe(true);
    expect(isActive('/modelsx', { href: '/models' })).toBe(false);
    expect(isActive('/tools/judgments', { href: '/tools/playground', match: '/tools' })).toBe(true);
  });
});

describe('SubNav', () => {
  it('renders the Tools tabs', () => {
    withRouter(<SubNav items={TOOLS_TABS} label="Tools" />, '/tools/benchmark');
    expect(screen.getByText('Benchmark').getAttribute('aria-current')).toBe('page');
    expect(screen.getByText('Playground').getAttribute('aria-current')).toBeNull();
  });
});

describe('AlertBand', () => {
  it('orders alerts most severe first and hides when empty', () => {
    const { container, rerender } = withRouter(
      <AlertBand
        alerts={[
          { id: 'a', severity: 'info', message: 'info msg', actions: [] },
          { id: 'b', severity: 'critical', message: 'critical msg', actions: [] },
          { id: 'c', severity: 'warn', message: 'warn msg', actions: [] },
        ]}
      />,
    );
    const items = Array.from(container.querySelectorAll('.alertband-item')).map((n) => n.getAttribute('data-severity'));
    expect(items).toEqual(['critical', 'warn', 'info']);
    expect(container.querySelector('[data-severity="critical"]')?.getAttribute('role')).toBe('alert');
    rerender(<AlertBand alerts={[]} />);
    expect(container.querySelector('.alertband')).toBeNull();
  });

  it('renders actions, the count, and Dismiss only for non-critical dismissible alerts', () => {
    const onAction = vi.fn();
    const onDismiss = vi.fn();
    const restart = { id: 'restart', label: 'Restart engine', method: 'POST' as const, path: '/engine/restart' };
    withRouter(
      <AlertBand
        onAction={onAction}
        onDismiss={onDismiss}
        alerts={[
          { id: 'engine_failed', severity: 'critical', title: 'Engine stopped.', message: 'Repeated failures', actions: [restart] },
          { id: 'queue_full', severity: 'warn', message: 'Queue full (32)', count: 3, actions: [] },
          { id: 'sticky', severity: 'info', message: 'Pinned notice', dismissible: false, actions: [] },
        ]}
      />,
    );
    expect(screen.getByText('Engine stopped.')).toBeTruthy();
    expect(screen.getByText(/×3 · last/)).toBeTruthy();
    fireEvent.click(screen.getByText('Restart engine'));
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ id: 'engine_failed' }), restart);
    const dismiss = screen.getAllByText('Dismiss');
    expect(dismiss).toHaveLength(1);
    fireEvent.click(dismiss[0]!);
    expect(onDismiss).toHaveBeenCalledWith(expect.objectContaining({ id: 'queue_full' }));
  });
});

describe('Toggle', () => {
  it('is a switch that flips', () => {
    const onChange = vi.fn();
    render(<Toggle checked={false} onChange={onChange} label="Persistent cache" />);
    const sw = screen.getByRole('switch', { name: 'Persistent cache' });
    expect(sw.getAttribute('aria-checked')).toBe('false');
    fireEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });
});

describe('Field', () => {
  it('shows flag verbatim, source chip, restart badge, error and the ? disclosure', () => {
    render(
      <Field label="SSD cache" help="0 disables." flag="--max-cache-disk" source="model" restart error="Bad size" more={<p>Deeper text</p>}>
        {({ id, describedBy }) => <input id={id} aria-describedby={describedBy} />}
      </Field>,
    );
    expect(screen.getByText('--max-cache-disk')).toBeTruthy();
    expect(screen.getByText('This model')).toBeTruthy();
    expect(screen.getByText('Restart')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toBe('Bad size');
    const input = screen.getByLabelText('SSD cache');
    expect(input.getAttribute('aria-describedby')?.split(' ')).toHaveLength(2);
    expect(screen.queryByText('Deeper text')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'More about SSD cache' }));
    expect(screen.getByText('Deeper text')).toBeTruthy();
  });
});

describe('SizeInput', () => {
  it('validates like Splash and shows the parsed size', () => {
    function Harness() {
      const [v, setV] = useState('32G');
      return <SizeInput kind="max-cache-disk" value={v} onChange={setV} id="s" />;
    }
    render(<Harness />);
    expect(screen.getByText('= 32 GB')).toBeTruthy();
    const input = screen.getByRole('textbox') as HTMLInputElement;
    fireEvent.input(input, { target: { value: '0' } });
    expect(screen.getByText('Disabled')).toBeTruthy();
    fireEvent.input(input, { target: { value: '1.5G' } });
    expect(screen.getByText('use 0 to disable, or a size such as 5G')).toBeTruthy();
    expect(input.getAttribute('aria-invalid')).toBe('true');
  });
});

describe('SizeInput suffix and description', () => {
  it('puts the suffix on the parsed-size line and keeps the field description', () => {
    render(
      <>
        <p id="help">Help</p>
        <SizeInput kind="max-memory" value="40G" onChange={() => {}} id="m" aria-describedby="help" suffix={() => '63% of 64 GB'} />
      </>,
    );
    expect(screen.getAllByText(/= 40/)).toHaveLength(1);
    expect(screen.getByText('= 40 GB · 63% of 64 GB')).toBeTruthy();
    expect(screen.getByRole('textbox').getAttribute('aria-describedby')).toBe('m-size help');
  });
});

describe('TagList', () => {
  it('adds on Enter, rejects duplicates and invalid values, removes', () => {
    function Harness() {
      const [v, setV] = useState<string[]>(['a']);
      return <TagList label="Add" values={v} onChange={setV} validate={(x) => (x.includes(' ') ? 'No spaces' : null)} />;
    }
    render(<Harness />);
    const input = screen.getByLabelText('Add');
    fireEvent.input(input, { target: { value: 'b' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByText('b')).toBeTruthy();
    fireEvent.input(input, { target: { value: 'a' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByRole('alert').textContent).toBe('Already in the list');
    fireEvent.input(input, { target: { value: 'x y' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByRole('alert').textContent).toBe('No spaces');
    fireEvent.click(screen.getByRole('button', { name: 'Remove a' }));
    expect(screen.queryByText('a')).toBeNull();
  });
});

describe('Sheet', () => {
  it('traps focus, closes on Escape and restores focus', () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>Open</button>
          <Sheet open={open} title="Details" onClose={() => setOpen(false)}>
            <input aria-label="inner" />
          </Sheet>
        </>
      );
    }
    render(<Harness />);
    const opener = screen.getByText('Open');
    opener.focus();
    fireEvent.click(opener);
    const dialog = screen.getByRole('dialog', { name: 'Details' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    const close = screen.getByText('Close');
    const inner = screen.getByLabelText('inner');
    // Focus moves to the sheet title first (docs/ui/00 §4.3).
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Details' }));
    close.focus();
    inner.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(inner);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

describe('StickySaveBar', () => {
  it('labels changes and the restart variant', () => {
    const onSave = vi.fn();
    const { rerender, container } = render(<StickySaveBar changes={1} onSave={onSave} onDiscard={() => undefined} />);
    expect(screen.getByText('1 change')).toBeTruthy();
    fireEvent.click(screen.getByText('Save'));
    expect(onSave).toHaveBeenCalled();
    rerender(<StickySaveBar changes={3} restart onSave={onSave} onDiscard={() => undefined} />);
    expect(screen.getByText('3 changes')).toBeTruthy();
    expect(screen.getByText('Save & restart engine')).toBeTruthy();
    rerender(<StickySaveBar changes={0} onSave={onSave} onDiscard={() => undefined} />);
    expect(container.querySelector('.savebar')).toBeNull();
  });
});

describe('CopyButton', () => {
  it('flips COPY to COPIED', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<CopyButton text="http://127.0.0.1:8000/v1" />);
    fireEvent.click(screen.getByText('Copy'));
    expect(await screen.findByText('Copied')).toBeTruthy();
    expect(writeText).toHaveBeenCalledWith('http://127.0.0.1:8000/v1');
  });
});

describe('ListRow and ProgressBar', () => {
  it('renders name, label and actions', () => {
    withRouter(
      <ul>
        <ListRow name="Qwen3.8-27B-4bit" label="01 — MLX · 15.6 GB" href="/models" actions={<button>Load</button>} />
      </ul>,
    );
    expect(screen.getByRole('link', { name: 'Qwen3.8-27B-4bit' })).toBeTruthy();
    expect(screen.getByText('01 — MLX · 15.6 GB')).toBeTruthy();
    expect(screen.getByText('Load')).toBeTruthy();
  });

  it('clamps progress and supports indeterminate', () => {
    const { rerender } = render(<ProgressBar value={1.4} label="Download" />);
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100');
    rerender(<ProgressBar value={null} label="Download" />);
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBeNull();
    expect(screen.getByRole('progressbar').getAttribute('data-indeterminate')).toBe('true');
  });
});
