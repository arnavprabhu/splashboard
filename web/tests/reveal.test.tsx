/**
 * SPEC §18.4 and DESIGN.md Motion: reveal-on-scroll on the long pages only. A block below the fold
 * eases in when it enters the viewport; content visible at load is never hidden.
 */
import { render } from '@testing-library/preact';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RevealBlock } from '../src/components/RevealBlock';

type Entry = { isIntersecting: boolean; target: Element };

let observer: { cb: (entries: Entry[]) => void; observed: Element[]; disconnected: boolean } | null = null;

class FakeIntersectionObserver {
  observed: Element[] = [];
  disconnected = false;
  constructor(public cb: (entries: Entry[]) => void) {
    observer = this;
  }
  observe(el: Element) {
    this.observed.push(el);
  }
  unobserve(el: Element) {
    this.observed = this.observed.filter((x) => x !== el);
  }
  disconnect() {
    this.disconnected = true;
  }
}

function placeAt(top: number) {
  return vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({ top, bottom: top + 40, left: 0, right: 0, width: 0, height: 40, x: 0, y: top, toJSON: () => ({}) } as DOMRect);
}

afterEach(() => {
  vi.unstubAllGlobals();
  observer = null;
});

describe('RevealBlock (SPEC §18.4)', () => {
  it('eases a block that starts below the fold in when it enters the viewport', () => {
    vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
    placeAt(window.innerHeight + 200);
    const { container } = render(
      <RevealBlock>
        <p>Late section</p>
      </RevealBlock>,
    );
    const block = container.firstElementChild as HTMLElement;
    expect(block.classList.contains('reveal')).toBe(true);
    expect(observer?.observed).toEqual([block]);

    observer!.cb([{ isIntersecting: false, target: block }]);
    expect(block.classList.contains('is-visible')).toBe(false);
    observer!.cb([{ isIntersecting: true, target: block }]);
    expect(block.classList.contains('is-visible')).toBe(true);
    expect(observer?.observed).toEqual([]);
  });

  it('never hides a block that is visible at load', () => {
    vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
    placeAt(120);
    const { container } = render(
      <RevealBlock>
        <p>Top section</p>
      </RevealBlock>,
    );
    const block = container.firstElementChild as HTMLElement;
    expect(block.classList.contains('reveal')).toBe(false);
    expect(observer).toBeNull();
  });

  it('does nothing without IntersectionObserver, and the content still renders', () => {
    vi.stubGlobal('IntersectionObserver', undefined);
    placeAt(window.innerHeight + 200);
    const { container } = render(
      <RevealBlock>
        <p>Plain section</p>
      </RevealBlock>,
    );
    expect(container.textContent).toContain('Plain section');
    expect(container.querySelector('.reveal')).toBeNull();
  });

  it('disconnects the observer on unmount', async () => {
    vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
    placeAt(window.innerHeight + 200);
    const { unmount } = render(
      <RevealBlock>
        <p>Late section</p>
      </RevealBlock>,
    );
    const seen = observer;
    unmount();
    // Preact runs passive effect cleanups after paint, not during unmount.
    await vi.waitFor(() => expect(seen?.disconnected).toBe(true));
  });
});
