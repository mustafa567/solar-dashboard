import { useEffect, useRef, useState } from "react";
import DayChart from "./DayChart.jsx";
import PeriodBars from "./PeriodBars.jsx";
import { EmptyState, ErrorState, LoadingState, Panel, Skeleton } from "./States.jsx";
import { exportUrl } from "../lib/api.js";
import { formatKwh, formatPct, todayIso } from "../lib/format.js";

// Day / Month / Year. A week view was built and dropped: at this site the
// month view already answers "how did the last few days go", and the extra tab
// only added a decision. The API still supports range=week if it is ever wanted
// back.
const RANGES = [
  { id: "day", label: "Day" },
  { id: "month", label: "Month" },
  { id: "year", label: "Year" },
];

function RangeTabs({ range, onChange }) {
  return (
    <div
      className="flex rounded-xl border border-hairline bg-panel p-0.5"
      role="tablist"
      aria-label="Time range"
    >
      {RANGES.map((option) => {
        const active = range === option.id;
        return (
          <button
            key={option.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(option.id)}
            className={`flex-1 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
              active
                ? "bg-panel-raised text-ink"
                : "text-ink-muted hover:text-ink"
            }`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function Chevron({ direction }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={direction === "prev" ? "M15 18 L9 12 L15 6" : "M9 18 L15 12 L9 6"} />
    </svg>
  );
}

/** The day range's label arrives as an ISO date; show it as one. */
function displayLabel(label) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(label ?? "");
  if (!match) return label;
  const [, year, month, day] = match.map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

function PeriodNav({ label, onPrev, onNext, nextDisabled, onToday, showToday }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <button
        type="button"
        onClick={onPrev}
        aria-label="Previous period"
        title="Previous period (Left arrow)"
        className="rounded-lg border border-hairline p-2 text-ink-muted transition-colors hover:bg-panel-raised hover:text-ink"
      >
        <Chevron direction="prev" />
      </button>

      <div className="min-w-0 text-center">
        <p className="truncate text-sm font-medium text-ink">{label}</p>
        {showToday && (
          <button
            type="button"
            onClick={onToday}
            className="mt-0.5 text-xs text-ink-faint underline decoration-dotted hover:text-ink-muted"
          >
            Back to today
          </button>
        )}
      </div>

      <button
        type="button"
        onClick={onNext}
        disabled={nextDisabled}
        aria-label="Next period"
        title="Next period (Right arrow)"
        className="rounded-lg border border-hairline p-2 text-ink-muted transition-colors hover:bg-panel-raised hover:text-ink disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
      >
        <Chevron direction="next" />
      </button>
    </div>
  );
}

const RANGE_KEYS = { d: "day", m: "month", y: "year" };

/**
 * Left/right step through periods, T returns to today, D/M/Y pick the range.
 * Ignored while typing or with a modifier held, so browser shortcuts still work.
 */
function useAnalyzeKeys({ history, range, onChange }) {
  const stateRef = useRef({ history, range, onChange });
  stateRef.current = { history, range, onChange };

  useEffect(() => {
    const onKey = (event) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      const tag = event.target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      const { history: current, range: currentRange, onChange: change } = stateRef.current;
      const key = event.key.toLowerCase();

      if (key === "arrowleft" && current) {
        change({ range: currentRange, date: current.previous_date });
      } else if (key === "arrowright" && current && !current.is_current_period) {
        change({ range: currentRange, date: current.next_date });
      } else if (key === "t") {
        change({ range: currentRange, date: todayIso() });
      } else if (RANGE_KEYS[key]) {
        change({ range: RANGE_KEYS[key], date: current?.date ?? todayIso() });
      } else {
        return;
      }
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

function Kbd({ children }) {
  return (
    <kbd className="readout rounded border border-hairline bg-panel px-1 py-px text-[0.625rem] text-ink-muted">
      {children}
    </kbd>
  );
}

function ImportedDayCard({ totals }) {
  return (
    <Panel className="px-6 py-8 text-center">
      <p className="eyebrow text-ink-faint">From your monthly report</p>
      <p className="mt-3 text-sm text-ink-muted">
        There is no hour-by-hour detail for this day -- the poller was not
        running yet. The monthly report gives the day&apos;s totals:
      </p>
      <div className="mt-5 flex flex-wrap items-baseline justify-center gap-x-8 gap-y-3">
        <span>
          <span
            className="readout text-2xl font-semibold"
            style={{ color: "var(--color-solar)" }}
          >
            {formatKwh(totals.solar_kwh)}
          </span>
          <span className="ml-1 text-xs text-ink-faint">kWh solar</span>
        </span>
        <span>
          <span
            className="readout text-2xl font-semibold"
            style={{ color: "var(--color-home-bright)" }}
          >
            {formatKwh(totals.home_kwh)}
          </span>
          <span className="ml-1 text-xs text-ink-faint">kWh used</span>
        </span>
      </div>
      {totals.home_kwh == null && (
        <p className="mx-auto mt-4 max-w-sm text-xs leading-relaxed text-ink-faint">
          The report&apos;s usage figure for this day was negative, which cannot
          be true, so it is left out rather than shown as fact.
        </p>
      )}
    </Panel>
  );
}

/**
 * A nudge for the current period, which is nearly empty by definition when the
 * poller has only just started. Without it, "September" looks like the import
 * failed -- when in fact the reports simply stop at the end of last month.
 */
function ThinPeriodHint({ history, onPrev }) {
  const withData = history.points.filter((point) => point.has_data).length;
  const sparse = withData > 0 && withData <= history.points.length / 3;
  if (!history.is_current_period || !sparse) return null;

  return (
    <p className="text-center text-xs text-ink-faint">
      Only {withData} of {history.points.length} recorded so far this period.{" "}
      <button
        type="button"
        onClick={onPrev}
        className="underline decoration-dotted hover:text-ink-muted"
      >
        See the previous one
      </button>
      , which has your imported history.
    </p>
  );
}

function SummaryStat({ label, value, unit, accent, hint }) {
  return (
    <div className="px-4 py-3.5">
      <p className="eyebrow text-ink-faint" style={{ fontSize: "0.625rem" }}>
        {label}
      </p>
      <p className="readout mt-1.5 text-xl font-semibold" style={{ color: accent }}>
        {value}
        {unit && (
          <span className="ml-1 text-xs font-medium text-ink-faint">{unit}</span>
        )}
      </p>
      {hint && <p className="mt-0.5 text-[0.6875rem] text-ink-faint">{hint}</p>}
    </div>
  );
}

function SummaryGrid({ totals }) {
  // The monthly reports give solar and household totals but no directional
  // split, so for imported periods the grid figures are unknown rather than
  // zero -- and anything derived from them would read a confident 100%.
  const gridKnown = totals.grid_known !== false;

  return (
    <Panel className="grid grid-cols-2 divide-x divide-y divide-hairline sm:grid-cols-4 sm:divide-y-0">
      <SummaryStat
        label="Solar generated"
        value={formatKwh(totals.solar_kwh)}
        unit="kWh"
        accent="var(--color-solar)"
      />
      <SummaryStat
        label="Total consumed"
        value={formatKwh(totals.home_kwh)}
        unit="kWh"
        accent="var(--color-home-bright)"
      />
      <SummaryStat
        label="From grid"
        value={gridKnown ? formatKwh(totals.grid_import_kwh) : "--"}
        unit={gridKnown ? "kWh" : ""}
        accent="var(--color-grid)"
        hint={
          gridKnown
            ? `${formatKwh(totals.grid_export_kwh)} kWh exported`
            : "not in the monthly reports"
        }
      />
      <SummaryStat
        label="Self-consumption"
        value={gridKnown ? formatPct(totals.self_consumption_pct) : "--"}
        accent="var(--color-ink)"
        hint={
          gridKnown ? "of solar used on site" : "needs grid data to calculate"
        }
      />
    </Panel>
  );
}

export default function AnalyzeView({ history, error, loading, range, date, onChange, onRetry }) {
  const [copied, setCopied] = useState(false);

  const setRange = (nextRange) => onChange({ range: nextRange, date });
  useAnalyzeKeys({ history, range, onChange });

  if (error && !history) {
    return (
      <div className="space-y-4">
        <RangeTabs range={range} onChange={setRange} />
        <ErrorState error={error} onRetry={onRetry} />
      </div>
    );
  }

  if (loading && !history) {
    return (
      <div className="space-y-4">
        <RangeTabs range={range} onChange={setRange} />
        <Skeleton className="h-10" />
        <LoadingState label="Building the series" />
      </div>
    );
  }

  if (!history) return null;

  const isDay = history.range === "day";
  const notToday = history.date !== todayIso();

  return (
    <div className="space-y-4">
      <RangeTabs range={range} onChange={setRange} />

      <PeriodNav
        label={displayLabel(history.label)}
        onPrev={() => onChange({ range, date: history.previous_date })}
        onNext={() => onChange({ range, date: history.next_date })}
        nextDisabled={history.is_current_period}
        onToday={() => onChange({ range, date: todayIso() })}
        showToday={notToday}
      />

      {!history.has_data ? (
        history.imported_day_total ? (
          <ImportedDayCard totals={history.imported_day_total} />
        ) : (
          <EmptyState
            title="No data yet for this range"
            body={
              history.is_current_period
                ? "Readings are still being collected. Come back once the poller has been running for a while."
                : "The poller was not recording during this period, so there is nothing to chart. History only exists from the moment the service started."
            }
          />
        )
      ) : (
        <>
          <Panel className="px-2 py-4 sm:px-4">
            {isDay ? (
              <DayChart points={history.points} />
            ) : (
              <PeriodBars points={history.points} />
            )}
          </Panel>

          <SummaryGrid totals={history.totals} />
          <ThinPeriodHint
            history={history}
            onPrev={() => onChange({ range, date: history.previous_date })}
          />
        </>
      )}

      <div className="flex flex-wrap items-center justify-center gap-3 pt-1 text-xs">
        <a
          href={exportUrl(history.start, history.end, "csv")}
          className="rounded-lg border border-hairline px-3 py-1.5 text-ink-muted transition-colors hover:bg-panel-raised hover:text-ink"
        >
          Download this period as CSV
        </a>
        <button
          type="button"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(
                window.location.origin + exportUrl(history.start, history.end, "json"),
              );
              setCopied(true);
              setTimeout(() => setCopied(false), 2000);
            } catch {
              setCopied(false);
            }
          }}
          className="text-ink-faint underline decoration-dotted hover:text-ink-muted"
        >
          {copied ? "Link copied" : "Copy JSON export link"}
        </button>
      </div>

      <p className="hidden text-center text-[0.6875rem] text-ink-faint sm:block">
        <Kbd>&larr;</Kbd> <Kbd>&rarr;</Kbd> browse &middot; <Kbd>T</Kbd> today &middot;{" "}
        <Kbd>D</Kbd> <Kbd>M</Kbd> <Kbd>Y</Kbd> range
      </p>

      <p className="text-center text-xs leading-relaxed text-ink-faint">
        {history.timezone} &middot; totals integrated from{" "}
        {history.points.reduce((sum, point) => sum + point.sample_count, 0)} samples
        {history.imported_points > 0 && (
          <>
            <br />
            {history.imported_points === history.points.length
              ? "All of this period comes from your SunPower monthly reports"
              : `${history.imported_points} of ${history.points.length} periods come from your SunPower monthly reports`}
            , not from live polling. Grid import and export are not broken out
            in those.
          </>
        )}
      </p>
    </div>
  );
}
