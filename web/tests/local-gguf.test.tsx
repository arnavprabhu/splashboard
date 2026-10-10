/**
 * Local GGUF drop-in on the Models page: the Local tag, the drop note with the
 * real models folder, Rescan folder, and one plain line per file that was not added.
 */
import { fireEvent, render, screen } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { InstalledModel } from '../src/api/models';
import { toastQueue } from '../src/components/Toast';
import { LocalDrop } from '../src/routes/models/LocalDrop';
import { isLocalId, localErrors } from '../src/routes/models/local';
import { ManagerRow } from '../src/routes/models/ManagerRow';
import { engine } from '../src/store';

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const DIR = '/Users/arnav/.splash/models';
const LOCAL_ID = 'local/Qwen3.6-35B-A3B-UD-Q4_K_XL-GGUF';
const VIEW = {
  files: {
    'Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf': { model: LOCAL_ID, family: 'Qwen3.6-35B-A3B', draft: 'incoai/Qwen3.6-35B-A3B-DFlash2', language_only: true },
    'llama-3-8b.Q4_K_M.gguf': { error: 'unsupported GGUF model architecture: llama' },
    'Qwen3.5-9B-Q4_K_M.gguf': { error: 'no supported model has this architecture' },
  },
  ignored: [],
};

beforeEach(() => {
  toastQueue.value = [];
  engine.value = { state: 'stopped', model: null } as never;
});

describe('logic', () => {
  it('isLocalId matches only the local/ owner', () => {
    expect(isLocalId(LOCAL_ID)).toBe(true);
    expect(isLocalId('unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL')).toBe(false);
    expect(isLocalId('localhost/x')).toBe(false);
  });
  it('localErrors keeps only failed files, sorted by name', () => {
    expect(localErrors(VIEW)).toEqual([
      { file: 'llama-3-8b.Q4_K_M.gguf', error: 'unsupported GGUF model architecture: llama' },
      { file: 'Qwen3.5-9B-Q4_K_M.gguf', error: 'no supported model has this architecture' },
    ]);
    expect(localErrors(null)).toEqual([]);
    expect(localErrors({ files: {}, ignored: [] })).toEqual([]);
  });
});

describe('LocalDrop', () => {
  it('shows the models folder and a line per file that was not added', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(200, VIEW));
    render(<LocalDrop modelsDir={DIR} onRescanned={() => undefined} />);
    const drop = screen.getByTestId('local-drop');
    expect(drop.textContent).toContain(`Drop a .gguf into ${DIR} and it is added automatically.`);
    expect(drop.querySelector('.mono')?.textContent).toBe(DIR);
    const lines = await screen.findAllByRole('listitem');
    expect(lines.map((li) => li.textContent)).toEqual([
      'llama-3-8b.Q4_K_M.ggufunsupported GGUF model architecture: llama',
      'Qwen3.5-9B-Q4_K_M.ggufno supported model has this architecture',
    ]);
    // The file that became a model is not an error line.
    expect(drop.textContent).not.toContain('Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf');
  });

  it('a failed GET leaves the note and action, with no error lines', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(501, { error: { message: 'no', type: 'not_implemented', code: 'not_implemented' } }));
    render(<LocalDrop modelsDir={DIR} onRescanned={() => undefined} />);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.getByTestId('local-rescan')).toBeTruthy();
    expect(screen.queryByTestId('local-errors')).toBeNull();
  });

  it('Rescan folder POSTs /models/local/rescan, refreshes the installed list and the lines', async () => {
    const seen: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      seen.push(`${init?.method ?? 'GET'} ${url}`);
      if (url.endsWith('/models/local/rescan')) {
        return json(200, { added: [LOCAL_ID], files: { 'llama-3-8b.Q4_K_M.gguf': VIEW.files['llama-3-8b.Q4_K_M.gguf'] }, ignored: [] });
      }
      return json(200, { files: {}, ignored: [] });
    });
    const onRescanned = vi.fn();
    render(<LocalDrop modelsDir={DIR} onRescanned={onRescanned} />);
    await vi.waitFor(() => expect(seen).toContain('GET /api/admin/models/local'));
    expect(screen.queryByTestId('local-errors')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Rescan folder' }));
    await vi.waitFor(() => expect(onRescanned).toHaveBeenCalledTimes(1));
    expect(seen).toContain('POST /api/admin/models/local/rescan');
    expect(screen.getByTestId('local-errors').textContent).toContain('unsupported GGUF model architecture: llama');
    expect(toastQueue.value.at(-1)?.message).toBe('Added Qwen3.6-35B-A3B-UD-Q4_K_XL-GGUF.');
  });

  it('a failed rescan reports it and does not refresh the list', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) =>
      String(input).endsWith('/rescan') ? json(503, { error: { message: 'Splash is not installed', type: 'overloaded_error', code: 'engine_missing' } }) : json(200, { files: {}, ignored: [] }),
    );
    const onRescanned = vi.fn();
    render(<LocalDrop modelsDir={null} onRescanned={onRescanned} />);
    expect(screen.getByTestId('local-drop').textContent).toContain('Drop a .gguf into the models folder');
    fireEvent.click(screen.getByRole('button', { name: 'Rescan folder' }));
    await vi.waitFor(() => expect(toastQueue.value.at(-1)).toMatchObject({ message: 'Could not rescan the models folder.', tone: 'error' }));
    expect(onRescanned).not.toHaveBeenCalled();
  });
});

describe('Local tag on Manager rows', () => {
  const model = (id: string) =>
    ({ id, repo_id: id, format: 'gguf', family: 'Qwen3.6-35B-A3B', language_only: true, size_bytes: 21e9, unique_bytes: 21e9, pinned: false, status: 'ready' }) as InstalledModel;
  const noop = () => undefined;
  const row = (id: string) =>
    render(<ManagerRow id={id} model={model(id)} index={1} active={false} onOpen={noop} onUnload={noop} onVerify={noop} onUpdate={noop} onDelete={noop} onCancel={noop} />);

  it('a local/ model has a Local tag and no Hugging Face link', () => {
    row(LOCAL_ID);
    const tag = [...document.querySelectorAll('.tag')].find((el) => el.textContent === 'Local');
    expect(tag?.getAttribute('data-tone')).toBe('mute');
    expect(screen.queryByText('View on Hugging Face')).toBeNull();
  });

  it('a Hub model has no Local tag', () => {
    row('unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL');
    expect([...document.querySelectorAll('.tag')].some((el) => el.textContent === 'Local')).toBe(false);
    expect(screen.getByText('View on Hugging Face')).toBeTruthy();
  });
});
