/**
 * The delete sheet for local/ models (SPEC §9.5, D67): the note that the .gguf is kept, the
 * unticked "Also move the file to the Trash" opt-in, the trash_source query, and a failed move.
 */
import { fireEvent, render, screen } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { InstalledModel } from '../src/api/models';
import { DeleteSheet } from '../src/routes/models/DeleteSheet';
import { toastQueue } from '../src/components/Toast';

const LOCAL = 'local/Qwen3.6-35B-A3B-UD-Q4_K_XL-GGUF';
const HUB = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M';

function model(id: string): InstalledModel {
  return {
    id,
    repo_id: id.split(':')[0],
    format: 'gguf',
    family: 'Qwen3.6-35B-A3B',
    language_only: true,
    size_bytes: 21e9,
    unique_bytes: 21e9,
    pinned: false,
    legacy: false,
    status: 'ready',
  } as InstalledModel;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function sheet(models: InstalledModel[], onDone = vi.fn()) {
  return render(
    <DeleteSheet open models={models} activeId={null} freeBytes={null} onClose={() => undefined} onDone={onDone} />,
  );
}

/** Confirms the sheet; models of 10 GB or more also need the typed word (DeleteSheet). */
function confirmDelete(): void {
  const typed = screen.queryByLabelText('Type DELETE to confirm');
  if (typed) fireEvent.input(typed, { target: { value: 'DELETE' } });
  fireEvent.click(screen.getByRole('button', { name: /^Delete ·/ }));
}

/** "DELETE <url>" for each fetch the spy saw that was a DELETE. */
function deleteCalls(spy: { mock: { calls: unknown[][] } }): string[] {
  return spy.mock.calls
    .map(([input, init]) => `${(init as RequestInit | undefined)?.method ?? 'GET'} ${String(input)}`)
    .filter((c) => c.startsWith('DELETE'));
}

beforeEach(() => {
  toastQueue.value = [];
});

describe('DeleteSheet for local models', () => {
  it('says the .gguf is kept and offers an unticked Trash opt-in', () => {
    sheet([model(LOCAL)]);
    expect(screen.getByTestId('delete-local-keep').textContent).toBe('The .gguf file is kept in the models folder.');
    const box = screen.getByRole('checkbox', { name: 'Also move the file to the Trash' }) as HTMLInputElement;
    expect(box.checked).toBe(false);
  });

  it('shows neither the note nor the opt-in for a Hub model', () => {
    sheet([model(HUB)]);
    expect(screen.queryByTestId('delete-local')).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('unticked: DELETE carries no trash_source', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(200, { deleted: [LOCAL], freed_bytes: 0, kept_draft: false, engine_stopped: false, trashed: [], trash_failed: [] }));
    sheet([model(LOCAL)]);
    confirmDelete();
    await vi.waitFor(() => expect(deleteCalls(fetch)).toHaveLength(1));
    expect(deleteCalls(fetch)[0]).not.toContain('trash_source');
  });

  it('ticked: DELETE asks for trash_source=true', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(200, { deleted: [LOCAL], freed_bytes: 0, kept_draft: false, engine_stopped: false, trashed: [`/Users/x/.splash/models/a.gguf`], trash_failed: [] }));
    sheet([model(LOCAL)]);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Also move the file to the Trash' }));
    confirmDelete();
    await vi.waitFor(() => expect(deleteCalls(fetch)).toHaveLength(1));
    expect(deleteCalls(fetch)[0]).toContain('trash_source=true');
  });

  it('a mixed selection sends trash_source only for the local model', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) =>
      json(200, { deleted: [String(input)], freed_bytes: 0, kept_draft: false, engine_stopped: false, trashed: [], trash_failed: [] }),
    );
    sheet([model(LOCAL), model(HUB)]);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Also move the file to the Trash' }));
    confirmDelete();
    await vi.waitFor(() => expect(deleteCalls(fetch)).toHaveLength(2));
    const calls = deleteCalls(fetch);
    expect(calls.find((c) => c.includes('Qwen3.6-35B-A3B-UD-Q4_K_XL-GGUF'))).toContain('trash_source=true');
    expect(calls.find((c) => c.includes('GGUF:UD-Q4_K_M'))).not.toContain('trash_source');
  });

  it('a failed move is reported and the model is still deleted', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json(200, { deleted: [LOCAL], freed_bytes: 0, kept_draft: false, engine_stopped: false, trashed: [], trash_failed: ['/Users/x/.splash/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf'] }),
    );
    const onDone = vi.fn();
    sheet([model(LOCAL)], onDone);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Also move the file to the Trash' }));
    confirmDelete();
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledWith([LOCAL]));
    expect(toastQueue.value.at(-1)).toMatchObject({
      message: 'Could not move Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf to the Trash. It is still in the models folder.',
      tone: 'error',
    });
  });
});
