import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from "react";
import TopBar from "./components/TopBar.jsx";
import NowView from "./components/NowView.jsx";
import SystemView from "./components/SystemView.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import { LoadingState } from "./components/States.jsx";
import { fetchHistory, fetchLive } from "./lib/api.js";
import { usePolledResource } from "./hooks/usePolledResource.js";
import { useRoute } from "./hooks/useRoute.js";
import { useTheme } from "./hooks/useTheme.js";
import { formatKw, todayIso } from "./lib/format.js";

// Used until the first /api/live response says what the poller's interval
// actually is; after that the page polls in step with the backend.
const FALLBACK_POLL_MS = 45_000;
// Never poll faster than this, whatever the backend is configured to.
const MIN_POLL_MS = 10_000;

// Recharts is most of the bundle and only Analyze draws charts, so it loads on
// first visit to that view rather than delaying the live readout.
const AnalyzeView = lazy(() => import("./components/AnalyzeView.jsx"));

const VIEW_TITLE = { now: "Now", analyze: "Analyze", system: "System" };

export default function App() {
  const [route, navigate] = useRoute();
  const { view } = route;
  const selection = useMemo(
    () => ({ range: route.range, date: route.date }),
    [route.range, route.date],
  );

  const [livePollMs, setLivePollMs] = useState(FALLBACK_POLL_MS);
  const live = usePolledResource(fetchLive, { intervalMs: livePollMs });

  const backendIntervalS = live.data?.poll_interval_seconds;
  useEffect(() => {
    if (backendIntervalS > 0) {
      setLivePollMs(Math.max(MIN_POLL_MS, backendIntervalS * 1000));
    }
  }, [backendIntervalS]);

  const { theme, mode: themeMode, cycleMode } = useTheme(live.data);

  const historyFetcher = useCallback(
    (options) => fetchHistory(selection, options),
    [selection],
  );

  // History refreshes while the current period is on screen, so today's chart
  // fills in as readings land. Past periods are static and are not polled, and
  // nothing is fetched at all while another view is showing.
  const historyIntervalMs = selection.date === todayIso() ? 120_000 : null;
  const history = usePolledResource(historyFetcher, {
    intervalMs: historyIntervalMs,
    deps: [selection.range, selection.date],
    enabled: view === "analyze",
  });

  // The tab title carries the headline number, so a pinned tab is a readout.
  useEffect(() => {
    const kw = live.data?.has_data ? `${formatKw(live.data.solar_kw)} kW solar - ` : "";
    document.title = `${view === "now" ? kw : ""}${VIEW_TITLE[view]} - Solar`;
  }, [view, live.data]);

  return (
    <div className="min-h-dvh">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-panel focus:px-3 focus:py-2 focus:text-sm"
      >
        Skip to content
      </a>
      <TopBar
        view={view}
        onViewChange={(next) => navigate({ view: next })}
        connected={live.data?.gateway_reachable ?? false}
        hasData={live.data?.has_data ?? false}
        theme={theme}
        themeMode={themeMode}
        onCycleTheme={cycleMode}
      />

      <main id="main" className="mx-auto max-w-3xl px-4 py-6 sm:px-6 sm:py-8">
        <ErrorBoundary resetKey={view}>
          {view === "now" && (
            <NowView
              live={live.data}
              error={live.error}
              loading={live.loading}
              onRetry={live.refresh}
            />
          )}
          {view === "analyze" && (
            <Suspense fallback={<LoadingState label="Loading charts" />}>
              <AnalyzeView
                history={history.data}
                error={history.error}
                loading={history.loading}
                range={selection.range}
                date={selection.date}
                onChange={(next) => navigate({ view: "analyze", ...next })}
                onRetry={history.refresh}
              />
            </Suspense>
          )}
          {view === "system" && <SystemView />}
        </ErrorBoundary>
      </main>
    </div>
  );
}
