import { describe, expect, it } from 'vitest';
import { installedVariants, variantModelId } from '../src/routes/models/logic';

const REPO = 'Hcompany/Holo4-27B-GGUF';

describe('variantModelId', () => {
  it('installs the one GGUF named for its quant alone as the bare repository', () => {
    const variants = [{ name: 'q4_k_m', files: ['q4_k_m.gguf'] }];
    expect(variantModelId(REPO, 'q4_k_m', variants)).toBe(REPO);
  });

  it('keeps the variant when the repository has several GGUFs or it names its ending', () => {
    const several = [
      { name: 'q4_k_m', files: ['q4_k_m.gguf'] },
      { name: 'q8_0', files: ['q8_0.gguf'] },
    ];
    expect(variantModelId(REPO, 'q4_k_m', several)).toBe(`${REPO}:q4_k_m`);
    expect(variantModelId(REPO, 'Q4_K_M', [{ name: 'Q4_K_M', files: ['Holo4-27B-Q4_K_M.gguf'] }])).toBe(`${REPO}:Q4_K_M`);
    expect(variantModelId(REPO, null, several)).toBe(REPO);
  });

  it('counts the bare installation as that variant installed', () => {
    const variants = [{ name: 'q4_k_m', files: ['q4_k_m.gguf'] }];
    expect(installedVariants(REPO, [{ id: REPO }], variants).has('q4_k_m')).toBe(true);
    expect(installedVariants(REPO, [], variants).has('q4_k_m')).toBe(false);
  });
});
