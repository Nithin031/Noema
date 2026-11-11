import { useCallback, useEffect, useRef, useState } from 'react';

export type Fetched<T> = { data: T | null; error: string | null; loading: boolean; reload: () => void };

/** Fetch once (and on `key` change). Keeps the last good data on refetch errors. */
export function useFetch<T>(load: () => Promise<T>, key: string | number = 0): Fetched<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadRef.current()
      .then((d) => { if (!cancelled) { setData(d); setError(null); } })
      .catch((e: unknown) => { if (!cancelled) setError(e instanceof Error ? e.message : 'request failed'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [key, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
