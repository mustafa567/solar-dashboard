import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Fetches a resource and keeps it fresh, without ever blanking the screen.
 *
 * On a refresh failure the previous data stays visible and `error` is set
 * alongside it, because a stale reading with an honest warning is more useful
 * on a wall dashboard than an empty panel.
 */
export function usePolledResource(
  fetcher,
  { intervalMs = null, deps = [], enabled = true } = {},
) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshedAt, setRefreshedAt] = useState(null);

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(async (signal) => {
    try {
      const next = await fetcherRef.current({ signal });
      if (signal?.aborted) return;
      setData(next);
      setError(null);
      setRefreshedAt(Date.now());
    } catch (cause) {
      if (cause.name === "AbortError" || signal?.aborted) return;
      setError(cause);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  // A new dep set is a new resource (a different day, say), so show the
  // loading state again rather than the previous period's numbers.
  useEffect(() => {
    setLoading(true);
    setData(null);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    // Disabled resources (a view that is not on screen) fetch nothing.
    if (!enabled) return undefined;

    const controller = new AbortController();
    load(controller.signal);

    if (!intervalMs) return () => controller.abort();

    const timer = setInterval(() => {
      // Skip polling while the tab is hidden; catch up on the way back.
      if (document.visibilityState === "visible") load(controller.signal);
    }, intervalMs);

    const onVisible = () => {
      if (document.visibilityState === "visible") load(controller.signal);
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      controller.abort();
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, intervalMs, enabled, ...deps]);

  const refresh = useCallback(() => load(), [load]);

  return { data, error, loading, refreshedAt, refresh };
}
