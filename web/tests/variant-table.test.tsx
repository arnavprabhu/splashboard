/**
 * The GGUF variant table (SPEC §9.1, D8, D98, D99): the quality tier column, a dash for a variant
 * Splash cannot load, and the vision projector row, labelled and never selectable.
 */
import { fireEvent, render, screen } from '@testing-library/preact';
import { describe, expect, it, vi } from 'vitest';
import type { VariantOut } from '../src/api/models';
import { DASH, formatBytes } from '../src/lib/format';
import { VariantTable } from '../src/routes/models/VariantTable';
import { isProjector, projectorRow } from '../src/routes/models/logic';

const Q4: VariantOut = {
  name: 'UD-Q4_K_M',
  size_bytes: 17_300_000_000,
  bits_per_weight: 4,
  quality: 'Balanced',
  loadable: true,
  reason: null,
  fit: 'fits',
  recommended: true,
  files: ['Qwen3.8-27B-UD-Q4_K_M.gguf'],
};

const Q8: VariantOut = {
  name: 'UD-Q8_K_XL',
  size_bytes: 29_400_000_000,
  bits_per_weight: 8,
  quality: null,
  loadable: false,
  reason: 'Splash has no kernels for this variant (UD-Q8_K_XL)',
  fit: null,
  recommended: false,
  files: ['Qwen3.8-27B-UD-Q8_K_XL.gguf'],
};

describe('GGUF variant table', () => {
  it('shows the quality tier, and a dash for a variant Splash cannot load', () => {
    render(<VariantTable variants={[Q4, Q8]} caption="Variants" />);
    expect(screen.getByText('Balanced')).toBeTruthy();
    const q8 = screen.getByText('UD-Q8_K_XL').closest('tr');
    expect(q8?.textContent).toContain(DASH);
    expect(q8?.textContent).not.toContain('Balanced');
  });

  it('lists the vision projector last, labelled, with no radio button and no download', () => {
    const onSelect = vi.fn();
    const onDownload = vi.fn();
    const projector = projectorRow({ projector: 'mmproj-BF16.gguf', projector_bytes: 902_822_624 });
    render(
      <VariantTable
        variants={[Q4]}
        projector={projector}
        selected="UD-Q4_K_M"
        onSelect={onSelect}
        onDownload={onDownload}
        caption="Variants"
      />,
    );
    const rows = screen.getAllByRole('row').slice(1); // skip the header row
    expect(rows).toHaveLength(2);
    const projectorText = rows[1]?.textContent ?? '';
    expect(projectorText).toContain('mmproj-BF16.gguf');
    expect(projectorText).toContain('Vision projector');
    expect(projectorText).toContain(formatBytes(902_822_624, { base: 1000 }));
    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((radio) => radio.value)).toEqual(['UD-Q4_K_M']);
    const downloads = screen.getAllByRole('button', { name: /download/i });
    expect(downloads).toHaveLength(1);
    fireEvent.click(downloads[0] as HTMLElement);
    expect(onDownload).toHaveBeenCalledWith(Q4);
  });

  it('builds no projector row without a projector, and names every mmproj file as one', () => {
    expect(projectorRow({ projector: null })).toBeNull();
    expect(projectorRow(null)).toBeNull();
    expect(isProjector('mmproj-BF16.gguf')).toBe(true);
    expect(isProjector('Ternary-Bonsai-2-27B-mmproj-BF16.gguf')).toBe(true);
    expect(isProjector('UD-Q4_K_M')).toBe(false);
  });
});
