import { describe, expect, it } from 'vitest';
import type { SearchResult } from '../src/api/models';
import { matchesFilter, searchFormat } from '../src/routes/models-downloader';
import { catalogSeed, checkpointOf, formatKind, formatLabel, rowLabel } from '../src/routes/models/logic';

const NVIDIA = 'nvidia/Qwen3.8-27B-NVFP4';

describe('NVFP4 and FP8 checkpoints are labelled apart from MLX', () => {
  it('a row with a checkpoint label shows it instead of MLX', () => {
    expect(formatLabel('mlx', NVIDIA, 'NVFP4 · Model Optimizer')).toBe('NVFP4 · Model Optimizer');
    expect(formatLabel('mlx', NVIDIA, 'NVFP4 · Model Optimizer', true)).toBe('NVFP4');
    expect(rowLabel(1, 'mlx', NVIDIA, 22 * 1000 ** 3, 1000, 'NVFP4 · Model Optimizer')).toBe('01 — NVFP4 · 22 GB');
    // The label never overrides a GGUF.
    expect(formatLabel('gguf', 'unsloth/Qwen3.8-27B-GGUF', 'NVFP4 · Model Optimizer')).toBe('GGUF');
  });

  it('an mlx row without a label is MLX, whatever its name says', () => {
    expect(formatKind('mlx', NVIDIA)).toBe('mlx');
    expect(formatKind('mlx', 'KCh3dRi4n/Qwen3.6-35B-A3B-NVIDIA-NVFP4')).toBe('mlx');
    expect(formatKind('mlx', 'mlx-community/Qwen3.6-35B-A3B-nvfp4')).toBe('mlx');
    expect(formatKind('mlx', 'mlx-community/Qwen3.8-27B-4bit')).toBe('mlx');
  });

  it('a download not installed yet is guessed from its ID, GGUF first', () => {
    expect(formatKind(null, NVIDIA)).toBe('nvfp4');
    expect(formatKind(null, 'someone/Qwen3.6-27B-FP8')).toBe('fp8');
    expect(formatKind(null, 'cdiamond/Qwen3.8-27B-iMatrix-NVFP4-MTP-GGUF')).toBe('gguf');
    expect(rowLabel(1, null, 'cdiamond/Qwen3.8-27B-iMatrix-NVFP4-MTP-GGUF', null)).toBe('01 — GGUF · —');
    expect(formatKind(null, 'mlx-community/Qwen3.6-35B-A3B-nvfp4')).toBe('mlx');
  });

  it('checkpointOf reads the label from any row and ignores everything else', () => {
    expect(checkpointOf({ checkpoint: 'FP8 · compressed-tensors' })).toBe('FP8 · compressed-tensors');
    expect(checkpointOf({ checkpoint: null })).toBeNull();
    expect(checkpointOf({})).toBeNull();
    expect(checkpointOf(null)).toBeNull();
  });

  it('the offline catalog lists both NVFP4 rows', () => {
    const ids = catalogSeed().families.flatMap((f) => f.groups.flatMap((g) => g.entries.map((e) => e.id)));
    expect(ids).toContain(NVIDIA);
    expect(ids).toContain('mlx-community/Qwen3.6-35B-A3B-nvfp4');
    const entries = catalogSeed().families.flatMap((f) => f.groups.flatMap((g) => g.entries));
    const nvidia = entries.find((e) => e.id === NVIDIA)!;
    expect(formatLabel(nvidia.format, nvidia.id, checkpointOf(nvidia), true)).toBe('NVFP4');
    const mlx = entries.find((e) => e.id === 'mlx-community/Qwen3.6-35B-A3B-nvfp4')!;
    expect(formatLabel(mlx.format, mlx.id, checkpointOf(mlx), true)).toBe('MLX');
  });
});

describe('Hub search finds NVFP4 and FP8 checkpoints', () => {
  const hit = (id: string, format_guess: SearchResult['format_guess'], checkpoint: string | null = null) =>
    ({ id, format_guess, tags: [], checkpoint }) as SearchResult;
  const nvidia = hit(NVIDIA, 'mlx', 'NVFP4 · Model Optimizer');
  const mlx = hit('mlx-community/Qwen3.8-27B-4bit', 'mlx');
  const gguf = hit('unsloth/Qwen3.8-27B-GGUF', 'gguf');

  it('names a checkpoint hit by its kind', () => {
    expect(searchFormat('mlx', NVIDIA, 'NVFP4 · Model Optimizer')).toBe('NVFP4');
    expect(searchFormat('mlx', 'someone/x', 'compressed-tensors')).toBe('compressed-tensors');
    // An mlx-tagged repository with NVFP4 in its name is MLX's own checkpoint.
    expect(searchFormat('mlx', 'KCh3dRi4n/Qwen3.6-35B-A3B-NVIDIA-NVFP4')).toBe('MLX');
  });

  it('the Format filter keeps MLX and the checkpoints apart', () => {
    const all = [nvidia, mlx, gguf];
    expect(all.filter((r) => matchesFilter(r, 'all'))).toEqual(all);
    expect(all.filter((r) => matchesFilter(r, 'mlx'))).toEqual([mlx]);
    expect(all.filter((r) => matchesFilter(r, 'checkpoint'))).toEqual([nvidia]);
    expect(all.filter((r) => matchesFilter(r, 'gguf'))).toEqual([gguf]);
  });
});
