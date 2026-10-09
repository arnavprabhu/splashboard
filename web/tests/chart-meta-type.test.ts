/**
 * Chart axes use the meta type (13px, weight 600).
 */
import { describe, expect, it } from 'vitest';
import { buildOptions, type ChartProps } from '../src/components/Chart';

describe('chart axes', () => {
  it('sets both axes to the meta type, 600 13px', () => {
    const props = { data: [[1, 2], [3, 4]], series: [{ label: 'a' }] } as unknown as ChartProps;
    const opts = buildOptions(document.createElement('div'), props, 320);
    const axes = opts.axes ?? [];
    expect(axes).toHaveLength(2);
    for (const axis of axes) expect((axis as { font?: string }).font).toBe('600 13px sans-serif');
  });
});
