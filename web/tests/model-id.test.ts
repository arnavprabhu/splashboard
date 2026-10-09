import { describe, expect, it } from 'vitest';
import { isModelId, modelIdError, modelIdFromParams, modelSettingsPath, REPO_ID_ERROR, splitModelId, VARIANT_ERROR } from '../src/lib/model-id';

describe('model ids', () => {
  it('validates full Hugging Face ids only', () => {
    expect(isModelId('mlx-community/Qwen3.8-27B-4bit')).toBe(true);
    expect(isModelId('unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL')).toBe(true);
    expect(isModelId('qwen27')).toBe(false);
    expect(isModelId('a/b/c')).toBe(false);
    expect(isModelId('_org/repo')).toBe(true);
  });
  it('rejects what splash/install/models.py rejects, with its messages', () => {
    for (const bad of ['x-/y', 'x/y.', 'a--b/c', 'a/b..c', 'a/b.git', `a/${'r'.repeat(97)}`, ' a/b']) {
      expect(modelIdError(bad)).toBe(REPO_ID_ERROR);
    }
    for (const bad of ['a/b:', 'a/b:-x', 'a/b:x..y', 'a/b:x:y', `a/b:${'v'.repeat(65)}`]) {
      expect(modelIdError(bad)).toBe(VARIANT_ERROR);
    }
    expect(modelIdError(`a/${'r'.repeat(96)}`)).toBeNull();
    expect(splitModelId('a/b:Q4_K_M')).toEqual({ repo: 'a/b', variant: 'Q4_K_M' });
    expect(splitModelId('a/b')).toEqual({ repo: 'a/b', variant: null });
  });
  it('builds and parses settings paths', () => {
    const id = 'unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL';
    expect(modelSettingsPath(id)).toBe('/models/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL/settings');
    expect(modelIdFromParams({ owner: 'unsloth', repo: 'Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL' })).toBe(id);
    expect(modelIdFromParams({ id: encodeURIComponent(id) })).toBe(id);
    expect(modelIdFromParams({ id: '%E0%A4%A' })).toBe('%E0%A4%A');
  });
});
