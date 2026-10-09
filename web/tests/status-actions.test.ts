/** Batch 4 (reviewer): a Load retried with force after a 409 install_in_progress toasts once. */
import { waitFor } from '@testing-library/preact';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../src/api/client';

const toast = vi.fn();
vi.mock('../src/components/Toast', () => ({ toast: (...a: unknown[]) => toast(...a), toastError: vi.fn() }));
const loadModel = vi.fn();
vi.mock('../src/routes/status/api', () => ({ loadModel: (...a: unknown[]) => loadModel(...a), restartEngine: vi.fn(), stopEngine: vi.fn() }));

const { runEngineCall } = await import('../src/routes/status/actions');
const { installQuestion } = await import('../src/lib/engine-install');

describe('runEngineCall', () => {
  it('shows the Loading toast once when the call is retried with force', async () => {
    const conflict = new ApiError(409, { message: 'installing', type: 'conflict_error', code: 'install_in_progress', details: { same_model: false } });
    loadModel.mockImplementation(async (_m: string, force: boolean) => {
      if (!force) throw conflict;
      return { state: 'starting', phase: 'loading', model: 'b/m', engine: {} };
    });
    const done = runEngineCall({ kind: 'load', model: 'b/m' });
    await waitFor(() => expect(installQuestion.value).not.toBeNull());
    installQuestion.value!(true);
    await done;
    expect(loadModel.mock.calls.map((c) => c[1])).toEqual([false, true]);
    expect(toast).toHaveBeenCalledTimes(1);
  });
});
