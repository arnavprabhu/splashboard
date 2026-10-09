/**
 * UI QA pass 2026-10-04 (known bugs 1–4): the wizard's Download button
 * after a cancel, the tier line, download sizes, and the engine install progress / confirmation.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/preact';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../src/api/client';
import type { Catalog, CatalogEntry, DownloadItem, PresetList } from '../src/api/models';
import { readEngineSummary } from '../src/api/types';
import { InstallProgress } from '../src/components/InstallProgress';
import { Cancelled, forcedAction, installOf, installQuestion, isInstallConflict, withInstallConfirm } from '../src/lib/engine-install';
import { engine } from '../src/store';
import { activeDownloadFor, recommendation, tierSentence } from '../src/routes/welcome/logic';

const MLX_27B = 'mlx-community/Qwen3.8-27B-4bit';
const GGUF = 'unsloth/Qwen3.6-35B-A3B-GGUF';

const INSTALL = { repo: MLX_27B, revision: '4c0d1e2a9b', files: 9, total_bytes: 19_930_000_000, done_bytes: 8_600_000_000, speed_bps: 84_000_000, eta_s: 135 };

function installingEngine(model = MLX_27B, install: unknown = INSTALL) {
  return readEngineSummary({ state: 'starting', phase: 'installing', model, install, engine: { found: true } });
}

afterEach(() => {
  engine.value = null;
  installQuestion.value?.(false);
});

describe('bug 1: only a live download blocks the wizard row', () => {
  type Item = { id: string; model: string; state: DownloadItem['state'] };
  const items: Item[] = [
    { id: 'd1', model: MLX_27B, state: 'cancelled' },
    { id: 'd2', model: GGUF, state: 'failed' },
  ];
  it('a cancelled or failed download leaves Download usable', () => {
    expect(activeDownloadFor(items, MLX_27B)).toBeNull();
    expect(activeDownloadFor(items, GGUF)).toBeNull();
  });
  it('queued, running, verifying and paused block it', () => {
    for (const state of ['queued', 'running', 'verifying', 'paused'] as const) {
      expect(activeDownloadFor([...items, { id: 'd3', model: MLX_27B, state }], MLX_27B)?.id).toBe('d3');
    }
  });
});

describe('bug 2: the tier reason is a sentence', () => {
  it('turns every tier the manager sends into a sentence (settings/presets.py recommend)', () => {
    expect(tierSentence('≥ 48 GB')).toBe('Picked for Macs with 48 GB of memory or more.');
    expect(tierSentence('≥ 36 GB')).toBe('Picked for Macs with 36 GB of memory or more.');
    expect(tierSentence('36–47 GB')).toBe('Picked for Macs with 36–47 GB of memory.');
    expect(tierSentence('24–35 GB')).toBe('Picked for Macs with 24–35 GB of memory.');
  });
  it('keeps full sentences, wraps anything else, and drops empty reasons', () => {
    expect(tierSentence("Splash's supported models need at least 24 GB.")).toBe("Splash's supported models need at least 24 GB.");
    expect(tierSentence('M5 tier')).toBe('Picked for this Mac’s memory tier (M5 tier).');
    expect(tierSentence('')).toBeNull();
    expect(tierSentence(undefined)).toBeNull();
  });
});

describe('bug 3: the wizard sizes a download by what it fetches', () => {
  const entry = (e: Partial<CatalogEntry>): CatalogEntry => ({ family: 'Qwen3.8-27B', format: 'mlx', installed: false, recommended: false, ...e }) as CatalogEntry;
  const catalog = {
    memory_bytes: 68_719_476_736,
    families: [
      {
        family: 'Qwen3.8-27B',
        label: 'Qwen3.8-27B',
        groups: [
          {
            format: 'mlx',
            entries: [
              entry({ id: MLX_27B, repo_id: MLX_27B, size_bytes: 15_000_000_000, download_bytes: 19_930_000_000 }),
              entry({
                id: GGUF,
                repo_id: GGUF,
                format: 'gguf',
                size_bytes: 22_000_000_000,
                download_bytes: 22_900_000_000,
                recommended_variant: 'UD-Q4_K_M',
                variants: [
                  { name: 'UD-Q4_K_M', size_bytes: 22_000_000_000, download_bytes: 22_900_000_000, language_only_download_bytes: 22_000_000_000 },
                  { name: 'UD-Q2_K_XL', size_bytes: 13_000_000_000 },
                ] as CatalogEntry['variants'],
              }),
            ],
          },
        ],
      },
    ],
  } as unknown as Catalog;
  const presets = (models: string[]): PresetList =>
    ({
      memory_bytes: 68_719_476_736,
      presets: [{ id: 'chat', label: 'Chat', description: '', settings: {}, recommendation: { primary: { model: models[0]!, note: '' }, alternatives: models.slice(1).map((model) => ({ model, note: '' })), reason: '≥ 48 GB' } }],
    }) as PresetList;

  it('uses download_bytes (target + draft + vision), not the weights', () => {
    const row = recommendation(presets([MLX_27B]), 'chat', catalog, [])!.rows[0]!;
    expect(row.downloadBytes).toBe(19_930_000_000);
    expect(row.sizeBytes).toBe(15_000_000_000);
  });
  it('a GGUF pick uses its variant\'s download_bytes; a variant without one only has weights', () => {
    const rows = recommendation(presets([`${GGUF}:UD-Q4_K_M`, `${GGUF}:UD-Q2_K_XL`]), 'chat', catalog, [])!.rows;
    expect(rows[0]!.downloadBytes).toBe(22_900_000_000);
    expect(rows[1]!.downloadBytes).toBeNull();
    expect(rows[1]!.sizeBytes).toBe(13_000_000_000);
  });
  it('a language-only pick uses language_only_download_bytes (no projector)', () => {
    const list = presets([`${GGUF}:UD-Q4_K_M`]);
    list.presets[0]!.recommendation.primary!.overrides = { language_only: true };
    expect(recommendation(list, 'chat', catalog, [])!.rows[0]!.downloadBytes).toBe(22_000_000_000);
  });
});

describe('bug 4: install progress', () => {
  it('reads EngineView.install only while installing', () => {
    expect(installOf(installingEngine())).toEqual(INSTALL);
    expect(installOf(readEngineSummary({ state: 'starting', phase: 'loading', model: MLX_27B, install: INSTALL, engine: {} }))).toBeNull();
    expect(installOf(installingEngine(MLX_27B, null))).toBeNull();
  });

  it('shows repo, files, bytes, speed, ETA and the cannot-resume warning', () => {
    render(<InstallProgress install={INSTALL} />);
    expect(screen.getByText(`${MLX_27B}@4c0d1e2`)).toBeTruthy();
    expect(screen.getByText(/9 files/)).toBeTruthy();
    expect(screen.getByText('8.6 GB of 19.9 GB · 84 MB/s · 2 m 15 s left')).toBeTruthy();
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('43');
    expect(screen.getByText(/restarts the file in progress from zero/)).toBeTruthy();
  });
});

describe('bug 4: Load / Restart / Switch during an install ask first', () => {
  const answer = async (go: boolean) => {
    await waitFor(() => expect(installQuestion.value).not.toBeNull());
    installQuestion.value!(go);
  };

  it('asks while installing, then sends force', async () => {
    engine.value = installingEngine();
    const run = vi.fn(async (force: boolean) => force);
    const out = withInstallConfirm(run, 'other/model');
    await answer(true);
    await expect(out).resolves.toBe(true);
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith(true);
  });

  it('No keeps the install going and calls nothing', async () => {
    engine.value = installingEngine();
    const run = vi.fn(async () => 'x');
    const out = withInstallConfirm(run);
    await answer(false);
    await expect(out).rejects.toBeInstanceOf(Cancelled);
    expect(run).not.toHaveBeenCalled();
  });

  it('installing with nothing to download (Splash verifying a local model) asks nothing', async () => {
    // Acceptance 2026-10-07: every real start passes through `installing`; with no
    // EngineView.install the manager answers no 409, so a Save & restart must not ask.
    engine.value = installingEngine('local/OrcaSAQ-2-27B-Uncensored-GGUF', null);
    const run = vi.fn(async (force: boolean) => force);
    await expect(withInstallConfirm(run)).resolves.toBe(false);
    expect(run).toHaveBeenCalledWith(false);
    expect(installQuestion.value).toBeNull();
  });

  it('Load of the model being installed: the manager answers 202, no question', async () => {
    engine.value = installingEngine();
    const run = vi.fn(async (force: boolean) => force);
    await expect(withInstallConfirm(run, MLX_27B)).resolves.toBe(false);
    expect(installQuestion.value).toBeNull();
  });

  it('an older manager’s 409 with details.same_model is success: no question, no forced retry', async () => {
    engine.value = installingEngine();
    const same = new ApiError(409, { message: 'installing', type: 'conflict_error', code: 'install_in_progress', details: { same_model: true } });
    const run = vi.fn(async (_force: boolean): Promise<string> => {
      throw same;
    });
    await expect(withInstallConfirm(run, MLX_27B)).resolves.toBeUndefined();
    expect(run.mock.calls).toEqual([[false]]);
    expect(installQuestion.value).toBeNull();
  });

  it('a 409 install_in_progress the page did not know about asks, then retries with force', async () => {
    engine.value = readEngineSummary({ state: 'ready', model: MLX_27B, engine: {} });
    const conflict = new ApiError(409, { message: 'installing', type: 'conflict_error', code: 'install_in_progress' });
    expect(isInstallConflict(conflict)).toBe(true);
    const run = vi.fn(async (force: boolean) => {
      if (!force) throw conflict;
      return 'loaded';
    });
    const out = withInstallConfirm(run, 'other/model');
    await answer(true);
    await expect(out).resolves.toBe('loaded');
    expect(run.mock.calls).toEqual([[false], [true]]);
  });

  it('other errors pass through without asking', async () => {
    const busy = new ApiError(409, { message: 'busy', type: 'conflict_error', code: 'model_switch_busy' });
    await expect(withInstallConfirm(async () => Promise.reject(busy))).rejects.toBe(busy);
    expect(installQuestion.value).toBeNull();
  });

  it('the sheet explains the file restarts, and Interrupt answers yes', async () => {
    engine.value = installingEngine();
    const out = withInstallConfirm(async (force) => force);
    const sheet = await screen.findByRole('alertdialog');
    expect(sheet.textContent).toContain('Interrupt the download.');
    expect(sheet.textContent).toContain(`Splash is downloading ${MLX_27B}`);
    expect(sheet.textContent).toContain('starts again from zero');
    expect(sheet.textContent).toContain('8.6 GB of 19.9 GB');
    fireEvent.click(screen.getByTestId('confirm-sheet-confirm'));
    await expect(out).resolves.toBe(true);
  });

  it('alert actions that load or restart get force the way each route takes it', () => {
    expect(forcedAction({ path: '/engine/load', body: { model: 'a/b' } })).toEqual({ path: '/engine/load', body: { model: 'a/b', force: true } });
    expect(forcedAction({ path: '/api/admin/engine/restart' })).toEqual({ path: '/api/admin/engine/restart?force=true' });
    expect(forcedAction({ path: '/engine/restart?x=1' })?.path).toBe('/engine/restart?x=1&force=true');
    expect(forcedAction({ path: '/engine/stop' })).toBeNull();
  });
});
