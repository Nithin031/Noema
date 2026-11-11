// Only what every page needs: is the daemon answering? (Pages fetch their own data.)
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, type Health } from '../api/noema';

export type Live = { loaded: boolean; offline: boolean; offlineSince: Date | null; health: Health | null };

const LiveContext = createContext<Live | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Live>({ loaded: false, offline: false, offlineSince: null, health: null });

  useEffect(() => {
    let alive = true;
    const check = async () => {
      let health: Health | null = null;
      try { health = await api.health(); } catch { health = null; }
      if (!alive) return;
      setState((s) => ({
        loaded: true, offline: !health, health: health ?? s.health,
        offlineSince: health ? null : s.offlineSince ?? new Date(),
      }));
    };
    void check();
    const id = window.setInterval(() => void check(), 15_000);
    return () => { alive = false; window.clearInterval(id); };
  }, []);

  const value = useMemo(() => state, [state]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useLive(): Live {
  const v = useContext(LiveContext);
  if (!v) throw new Error('useLive must be used inside <LiveProvider>');
  return v;
}
