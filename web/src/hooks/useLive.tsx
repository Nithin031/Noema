// One shared set of pollers for the whole app, so every screen reads the same
// live snapshot instead of each opening its own requests.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  api, liveApi, memeCenterApi,
  type BreakStatus, type DailySummary, type Detection, type Health, type InterventionFeedRow,
  type IntentRow, type OutcomeRow, type RealtimeStatus,
} from '../api/noema';

export type Live = {
  loaded: boolean;            // first round of requests has settled
  offline: boolean;           // daemon health request failed
  offlineSince: Date | null;
  health: Health | null;
  realtime: RealtimeStatus | null;
  detections: Detection[];
  feed: InterventionFeedRow[];
  breakStatus: BreakStatus | null;
  intent: IntentRow | null;
  today: DailySummary | null;
  outcomes: OutcomeRow[];
  corpusSize: number | null;
  refresh: () => Promise<void>;
};

const LiveContext = createContext<Live | null>(null);

const FAST_MS = 10_000;
const SLOW_MS = 60_000;

async function settle<T>(p: Promise<T>): Promise<T | null> {
  try { return await p; } catch { return null; }
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Omit<Live, 'refresh'>>({
    loaded: false, offline: false, offlineSince: null, health: null, realtime: null, detections: [],
    feed: [], breakStatus: null, intent: null, today: null, outcomes: [], corpusSize: null,
  });
  const offlineSince = useRef<Date | null>(null);
  const alive = useRef(true);

  const fast = useCallback(async () => {
    const [health, realtime, dets, feed, brk, intents] = await Promise.all([
      settle(api.health()), settle(api.realtime()), settle(api.detections(10)),
      settle(memeCenterApi.interventionFeed(20)), settle(liveApi.breakStatus()), settle(liveApi.intents(5)),
    ]);
    if (!alive.current) return;
    if (!health) offlineSince.current ??= new Date(); else offlineSince.current = null;
    setState((s) => ({
      ...s, loaded: true, offline: !health, offlineSince: offlineSince.current,
      health: health ?? s.health, realtime: realtime ?? s.realtime,
      detections: dets?.detections ?? s.detections, feed: feed?.interventions ?? s.feed,
      breakStatus: brk?.break ?? s.breakStatus,
      intent: intents?.intents?.length ? [...intents.intents].sort((a, b) => b.created_at.localeCompare(a.created_at))[0] : s.intent,
    }));
  }, []);

  const slow = useCallback(async () => {
    const [today, outcomes, stats] = await Promise.all([
      settle(liveApi.summary('today')), settle(liveApi.outcomes(500)), settle(memeCenterApi.stats()),
    ]);
    if (!alive.current) return;
    setState((s) => ({
      ...s, today: today?.daily ?? s.today, outcomes: outcomes?.outcomes ?? s.outcomes,
      corpusSize: stats?.assets ?? s.corpusSize,
    }));
  }, []);

  useEffect(() => {
    alive.current = true;
    void fast(); void slow();
    const a = window.setInterval(() => void fast(), FAST_MS);
    const b = window.setInterval(() => void slow(), SLOW_MS);
    return () => { alive.current = false; window.clearInterval(a); window.clearInterval(b); };
  }, [fast, slow]);

  const refresh = useCallback(async () => { await Promise.all([fast(), slow()]); }, [fast, slow]);
  const value = useMemo<Live>(() => ({ ...state, refresh }), [state, refresh]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useLive(): Live {
  const v = useContext(LiveContext);
  if (!v) throw new Error('useLive must be used inside <LiveProvider>');
  return v;
}
