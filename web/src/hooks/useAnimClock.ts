import { useEffect, useState } from 'react';

export const reducedMotion = () =>
  typeof window !== 'undefined' && typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** rAF clock in ms. Frozen at `frozenAt` when the user prefers reduced motion. */
export function useAnimClock(frozenAt = 3200): number {
  const [n, setN] = useState(frozenAt);
  useEffect(() => {
    if (reducedMotion()) return;
    let raf = 0;
    const tick = () => { setN(performance.now()); raf = requestAnimationFrame(tick); };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return n;
}
