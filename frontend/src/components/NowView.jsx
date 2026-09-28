import FlowDiagram from "./FlowDiagram.jsx";
import { EmptyState, ErrorState, LoadingState, Panel } from "./States.jsx";
import {
  formatAge,
  formatClock,
  formatKw,
  formatKwh,
  formatPct,
} from "../lib/format.js";

function TodayStat({ label, value, unit, accent }) {
  return (
    <div className="px-3 py-3 text-center">
      <p className="eyebrow text-ink-faint" style={{ fontSize: "0.625rem" }}>
        {label}
      </p>
      <p className="readout mt-1.5 text-lg font-semibold" style={{ color: accent }}>
        {value}
        <span className="ml-0.5 text-[0.6875rem] font-medium text-ink-faint">
          {unit}
        </span>
      </p>
    </div>
  );
}

function DataWarning({ live }) {
  if (!live?.data_warning) return null;
  return (
    <div className="rounded-xl border border-solar/40 bg-solar/5 px-4 py-3">
      <p className="text-sm font-medium text-solar">Meter readings disagree</p>
      <p className="mt-1 text-xs leading-relaxed text-ink-muted">
        {live.data_warning}
      </p>
    </div>
  );
}

function StaleBanner({ live }) {
  if (!live?.has_data) return null;
  if (!live.stale && live.gateway_reachable) return null;

  return (
    <div className="rounded-xl border border-alert/40 bg-alert/5 px-4 py-3">
      <p className="text-sm font-medium text-alert">
        Showing the last reading the gateway gave us
      </p>
      <p className="mt-1 text-xs text-ink-muted">
        Taken {formatAge(live.age_seconds)}. The poller keeps retrying, so this
        will catch up on its own once the PVS6 answers again.
      </p>
    </div>
  );
}

export default function NowView({ live, error, loading, onRetry }) {
  if (loading && !live) return <LoadingState label="Reading the gateway" />;
  if (error && !live) return <ErrorState error={error} onRetry={onRetry} />;

  if (live && !live.has_data) {
    return (
      <div className="space-y-4">
        <EmptyState
          title="No readings yet"
          body={
            live.gateway_reachable
              ? "The poller is running and the first sample should land within a minute."
              : "The poller cannot reach your PVS6 yet. Check PVS_HOST in .env and that the gateway is on the network."
          }
        />
        <p className="text-center text-xs text-ink-faint">
          Polling every {live.poll_interval_seconds}s
        </p>
      </div>
    );
  }

  const today = live?.today;

  return (
    <div className="space-y-5">
      <StaleBanner live={live} />
      <DataWarning live={live} />

      {/* The status sentence, then the number it explains. */}
      <div className="text-center">
        <p className="eyebrow text-ink-faint">Right now</p>
        <p className="mt-2 text-balance text-lg font-medium leading-snug text-ink sm:text-xl">
          {live.status_line}
        </p>
      </div>

      <div className="text-center">
        <p
          key={live.timestamp}
          className="value-enter readout text-6xl font-semibold leading-none text-home-bright sm:text-7xl"
        >
          {formatKw(live.home_kw)}
        </p>
        <p className="mt-2 text-sm text-ink-muted">
          <span className="text-ink-faint">kW</span> home usage
        </p>
      </div>

      <FlowDiagram live={live} />

      {today && (
        <Panel className="divide-x divide-hairline grid grid-cols-2 sm:grid-cols-4">
          <TodayStat
            label="Solar today"
            value={formatKwh(today.solar_kwh)}
            unit="kWh"
            accent="var(--color-solar)"
          />
          <TodayStat
            label="Used today"
            value={formatKwh(today.home_kwh)}
            unit="kWh"
            accent="var(--color-home-bright)"
          />
          <TodayStat
            label="From grid"
            value={formatKwh(today.grid_import_kwh)}
            unit="kWh"
            accent="var(--color-grid)"
          />
          <TodayStat
            label="Self-powered"
            value={formatPct(today.self_sufficiency_pct)}
            unit=""
            accent="var(--color-ink)"
          />
        </Panel>
      )}

      <footer className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 text-xs text-ink-faint">
        <span>Updated {formatClock(live.timestamp) ?? "--"}</span>
        <span aria-hidden="true">/</span>
        <span>Every {live.poll_interval_seconds}s</span>
        {live.source && (
          <>
            <span aria-hidden="true">/</span>
            <span>via {live.source}</span>
          </>
        )}
        {error && (
          <>
            <span aria-hidden="true">/</span>
            <span className="text-alert">refresh failed, retrying</span>
          </>
        )}
      </footer>
    </div>
  );
}
