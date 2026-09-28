import { useCallback, useMemo, useState } from "react";
import TopBar from "./components/TopBar.jsx";
import NowView from "./components/NowView.jsx";
import AnalyzeView from "./components/AnalyzeView.jsx";
import { fetchHistory, fetchLive } from "./lib/api.js";
import { usePolledResource } from "./hooks/usePolledResource.js";
import { useTheme } from "./hooks/useTheme.js";
import { todayIso } from "./lib/format.js";

// The live view refreshes on its own; the backend reports its poll interval so
// the two stay in step without hardcoding a number in two places.
const FALLBACK_POLL_MS = 45_000;

export default function App() {
  const [view, setView] = useState("now");
  const [selection, setSelection] = useState({ range: "day", date: todayIso() });

  const live = usePolledResource(fetchLive, { intervalMs: FALLBACK_POLL_MS });
  const { theme, mode: themeMode, cycleMode } = useTheme(live.data);

  const historyFetcher = useCallback(
    (options) => fetchHistory(selection, options),
    [selection],
  );

  // History refreshes while the current period is on screen, so today's chart
  // fills in as readings land. Past periods are static and are not polled.
  const historyIntervalMs = useMemo(
    () => (selection.date === todayIso() ? 120_000 : null),
    [selection],
  );

  const history = usePolledResource(historyFetcher, {
    intervalMs: historyIntervalMs,
    deps: [selection.range, selection.date],
  });

  return (
    <div className="min-h-dvh">
      <TopBar
        view={view}
        onViewChange={setView}
        connected={live.data?.gateway_reachable ?? false}
        hasData={live.data?.has_data ?? false}
        theme={theme}
        themeMode={themeMode}
        onCycleTheme={cycleMode}
      />

      <main className="mx-auto max-w-3xl px-4 py-6 sm:px-6 sm:py-8">
        {view === "now" ? (
          <NowView
            live={live.data}
            error={live.error}
            loading={live.loading}
            onRetry={live.refresh}
          />
        ) : (
          <AnalyzeView
            history={history.data}
            error={history.error}
            loading={history.loading}
            range={selection.range}
            date={selection.date}
            onChange={setSelection}
            onRetry={history.refresh}
          />
        )}
      </main>
    </div>
  );
}
