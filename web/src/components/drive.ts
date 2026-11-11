import { useEffect, useRef, useState } from 'react';
import { clamp } from '../lib/adapt';
import { reducedMotion } from '../hooks/useAnimClock';

export type Drive = { d: number; snap: number; t: number; rec: boolean };

const calm = (x: number) => 0.18 + 0.05 * Math.sin(x * 1.3);
const io = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2);

/** The reference's synthetic 14 s loop (calm, rising, snap, recovery). Demo route only. */
export function demoDrive(nowMs: number): Drive {
  const t = (nowMs / 1000) % 14;
  let d: number;
  if (t < 6) d = calm(t);
  else if (t < 10) { const c6 = calm(6); d = c6 + (0.92 - c6) * io((t - 6) / 4); }
  else { const e = io(clamp((t - 10.4) / 3)); d = 0.92 * (1 - e) + calm(t - 14) * e; }
  return { d, t, snap: t >= 10 && t < 11.5 ? (t - 10) / 1.5 : -1, rec: t >= 10 && t < 12.6 };
}

/** Live driver: eases the drawn tension toward the real drift score and
 *  plays the snap animation each time `snapKey` changes (a nudge fired). */
export function useLiveDrive(score: number | null, snapKey: string | null): Drive {
  const [drive, setDrive] = useState<Drive>({ d: score ?? 0.2, snap: -1, t: 0, rec: false });
  const target = useRef(score ?? 0.2);
  const cur = useRef(score ?? 0.2);
  const snapStart = useRef<number | null>(null);
  const lastKey = useRef<string | null>(snapKey);
  target.current = score ?? 0.2;

  useEffect(() => {
    if (snapKey && snapKey !== lastKey.current) snapStart.current = performance.now();
    lastKey.current = snapKey;
  }, [snapKey]);

  useEffect(() => {
    if (reducedMotion()) { setDrive({ d: score ?? 0.2, snap: -1, t: 0, rec: false }); return; }
    let raf = 0;
    const tick = () => {
      cur.current += (target.current - cur.current) * 0.04;
      const now = performance.now();
      let snap = -1;
      if (snapStart.current !== null) {
        const s = (now - snapStart.current) / 1500;
        if (s <= 1) snap = s; else snapStart.current = null;
      }
      setDrive({ d: cur.current, snap, t: snap >= 0 ? 10 + snap * 1.5 : 0, rec: false });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [score]);
  return drive;
}

