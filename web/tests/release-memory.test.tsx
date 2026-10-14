/** Release memory now, from the Status page's Memory band: shown while a model is loaded with its
 * weights in memory; a busy engine or no model reads as plain advice, not as an error. */
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../src/api/client';

const toast = vi.fn();
const toastError = vi.fn();
vi.mock('../src/components/Toast', () => ({ toast: (...a: unknown[]) => toast(...a), toastError: (...a: unknown[]) => toastError(...a) }));
const releaseMemory = vi.fn();
vi.mock('../src/routes/status/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/routes/status/api')>()),
  releaseMemory: () => releaseMemory(),
}));

const { MemoryBand, canReleaseMemory } = await import('../src/routes/status/Bands');
const { engine } = await import('../src/store');

const loaded = { weights: { released: false, idle_release_seconds: null, restores: 0 } };

beforeEach(() => {
  toast.mockReset();
  toastError.mockReset();
  releaseMemory.mockReset();
  engine.value = { state: 'ready', model: 'o/m' } as never;
});

describe('canReleaseMemory', () => {
  it('applies only while a model is loaded and its weights are in memory', () => {
    expect(canReleaseMemory('ready', loaded)).toBe(true);
    expect(canReleaseMemory('busy', loaded)).toBe(true);
    expect(canReleaseMemory('ready', null)).toBe(true);
    expect(canReleaseMemory('ready', { weights: { released: true } })).toBe(false);
    for (const state of ['idle_released', 'starting', 'stopped', 'stopping', 'crashed', null, undefined]) {
      expect(canReleaseMemory(state, loaded)).toBe(false);
    }
  });
});

describe('Release memory button', () => {
  it('releases and says the next request restores the weights', async () => {
    releaseMemory.mockResolvedValue({ released: true });
    render(<MemoryBand raw={loaded} stopped={false} />);
    fireEvent.click(screen.getByRole('button', { name: 'Release memory' }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Memory released. The next request restores the weights.'));
    expect(releaseMemory).toHaveBeenCalledTimes(1);
    expect(toastError).not.toHaveBeenCalled();
  });

  it('shows the manager’s busy and no-model advice as a plain toast', async () => {
    const cases: [string, string][] = [
      ['The engine is busy; try again when requests finish.', 'engine_busy'],
      ['No model is loaded.', 'engine_not_loaded'],
    ];
    for (const [message, code] of cases) {
      toast.mockReset();
      releaseMemory.mockRejectedValueOnce(new ApiError(409, { message, type: 'conflict_error', code }));
      const view = render(<MemoryBand raw={loaded} stopped={false} />);
      fireEvent.click(screen.getByRole('button', { name: 'Release memory' }));
      await waitFor(() => expect(toast).toHaveBeenCalledWith(message));
      view.unmount();
    }
    expect(toastError).not.toHaveBeenCalled();
  });

  it('reports any other failure as an error', async () => {
    releaseMemory.mockRejectedValue(new ApiError(502, { message: 'boom', type: 'api_error', code: 'engine_error' }));
    render(<MemoryBand raw={loaded} stopped={false} />);
    fireEvent.click(screen.getByRole('button', { name: 'Release memory' }));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Could not release memory', expect.any(ApiError)));
  });

  it('is absent once the weights are released or with no model loaded', () => {
    const view = render(<MemoryBand raw={{ weights: { released: true } }} stopped={false} />);
    expect(screen.queryByRole('button', { name: 'Release memory' })).toBeNull();
    view.unmount();
    engine.value = { state: 'idle_released', model: 'o/m' } as never;
    render(<MemoryBand raw={loaded} stopped={false} />);
    expect(screen.queryByRole('button', { name: 'Release memory' })).toBeNull();
  });
});
