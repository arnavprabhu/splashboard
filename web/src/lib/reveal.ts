import type { RefObject } from 'preact';
import { useEffect } from 'preact/hooks';

/**
 * Reveal-on-scroll for long pages only (SPEC §18.4: history and benchmark compare).
 * Elements already in view at mount are never hidden; reduced motion is handled in CSS.
 */
export function useReveal(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const rect = el.getBoundingClientRect();
    if (rect.top < window.innerHeight) return;
    el.classList.add('reveal');
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible');
            io.unobserve(entry.target);
          }
        }
      },
      { rootMargin: '0px 0px -10% 0px' },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);
}
