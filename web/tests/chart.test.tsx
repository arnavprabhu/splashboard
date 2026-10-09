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

import { Chart, xLabel } from '../src/components/Chart';

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

  it('labels day buckets in the hidden table with dates, not midnight times', () => {
    // Local midnight, as the manager's day buckets are.
    const day1 = new Date(2026, 9, 7).getTime() / 1000;
    const day2 = new Date(2026, 9, 8).getTime() / 1000;
    const { container } = render(
      <Chart data={[[day1, day2], [5, 7]] as never} series={[{ label: 'Requests', primary: true }]} days />,
    );
    const table = container.querySelector('table.visually-hidden')!;
    expect(table.querySelector('thead th')!.textContent).toBe('Day');
    const cells = [...table.querySelectorAll('tbody tr td:first-child')].map((td) => td.textContent);
    expect(cells).toEqual(['7 Oct 2026', '8 Oct 2026']);
    expect(table.textContent).not.toContain('00:00:00');
  });

  it('shows each stacked series its own value, not the running total', () => {
    const day = new Date(2026, 9, 7).getTime() / 1000;
    // Top of the stack first: model A on top of model B (300 = 200 + 100).
    const { container } = render(
      <Chart data={[[day], [300], [100]] as never} series={[{ label: 'A' }, { label: 'B' }]} ariaLabel="Tokens per day" days stacked bars />,
    );
    const table = container.querySelector('table.visually-hidden')!;
    expect(table.querySelector('caption')!.textContent).toBe('Tokens per day — last samples');
    expect([...table.querySelectorAll('tbody td')].map((td) => td.textContent)).toEqual(['7 Oct 2026', '200', '100']);
  });

  it('keeps times for sub-day samples', () => {
    const t = new Date(2026, 9, 7, 14, 5, 9).getTime() / 1000;
    expect(xLabel(t, {})).toBe('14:05:09');
    expect(xLabel(t, { days: true })).toBe('7 Oct 2026');
    expect(xLabel(3, { time: false })).toBe('3');
  });
});
