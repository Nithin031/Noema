import { useEffect, useRef, type ReactNode } from 'react';
import { zoomFor } from '../lib/zoom';

export default function Stage({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const apply = () => { if (ref.current) ref.current.style.setProperty('zoom', String(zoomFor(window.innerWidth))); };
    apply();
    window.addEventListener('resize', apply);
    return () => window.removeEventListener('resize', apply);
  }, []);
  return <div ref={ref}>{children}</div>;
}
