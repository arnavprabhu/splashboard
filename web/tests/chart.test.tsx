import { render } from '@testing-library/preact';
import { describe, expect, it, vi } from 'vitest';

const created = vi.hoisted(() => [] as unknown[]);
vi.mock('uplot', () => ({
  default: class {
    static paths = {};
    constructor(_opts: unknown, data: unknown) {
      created.push(data);
    }
    setData() {}
    setSize() {}
    destroy() {}
  },
}));

import { Chart } from '../src/components/Chart';

describe('Chart', () => {
  it('draws data that arrived while uPlot was still loading', async () => {
    const series = [{ label: 'Requests', primary: true }];
    const empty = [[], []] as never;
    const loaded = [[1, 2], [3, 4]] as never;
    // Usage history mounts the chart before its fetch answers; the answer can land before the
    // uPlot chunk does. The plot used to be built from the first render's (empty) data.
    const { rerender } = render(<Chart data={empty} series={series} />);
    rerender(<Chart data={loaded} series={series} />);
    await vi.waitFor(() => expect(created.length).toBe(1));
    expect(created[0]).toBe(loaded);
  });
});
