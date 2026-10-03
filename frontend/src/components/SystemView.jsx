import { useCallback, useEffect, useState } from "react";
import { ErrorState, LoadingState, Panel } from "./States.jsx";
import { fetchHistory, fetchStatus } from "../lib/api.js";
import { usePolledResource } from "../hooks/usePolledResource.js";
import {
  formatBytes,
  formatCount,
  formatDateTime,
  formatDuration,
  formatRelative,
  todayIso,
} from "../lib/format.js";

/*
  The health view. The gateway keeps no history, so every hour the poller
  misses is gone for good -- this page exists to make a silent gap visible
  while it is still minutes old rather than when a chart looks wrong a month
  later. It renders /api/status as reported; it does not second-guess it.
*/

const STATUS_POLL_MS = 15_000;
const COVERAGE_POLL_MS = 300_000;

// A re-render every few seconds keeps "42s ago" honest between polls.
function useNow(intervalMs = 5_000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

const TONE = {
  ok: { dot: "bg-grid", text: "text-grid", border: "border-grid/40", bg: "bg-grid/5" },
  warn: { dot: "bg-solar", text: "text-solar", border: "border-solar/40", bg: "bg-solar/5" },
  alert: { dot: "bg-alert", text: "text-alert", border: "border-alert/40", bg: "bg-alert/5" },
};

/** One headline for the whole service, worst condition first. */
function verdict(status, now) {
  const { poller, tasks, backups } = status;

  if (!poller.running) {
    return {
      tone: "alert",
      title: "The poller is not running",
      body: "No readings are being recorded. Restart the dashboard service.",
    };
  }
  if (!poller.gateway_reachable || poller.consecutive_failures > 0) {
    return {
      tone: "alert",
      title: "Cannot reach the gateway",
      body: `${poller.consecutive_failures} failed ${
        poller.consecutive_failures === 1 ? "attempt" : "attempts"
      } in a row. The poller keeps retrying with backoff; readings are missing until it answers.`,
    };
  }

  const lastSuccess = poller.last_success_at
    ? new Date(poller.last_success_at).getTime()
    : null;
  if (lastSuccess && now - lastSuccess > poller.current_interval_seconds * 3 * 1000) {
    return {
      tone: "warn",
      title: "Readings are late",
      body: `The last successful reading was ${formatRelative(poller.last_success_at, now)}.`,
    };
  }

  const failing = tasks.filter((task) => task.last_error);
  if (failing.length) {
    return {
      tone: "warn",
      title: `Background job failing: ${failing.map((task) => task.name).join(", ")}`,
      body: failing[0].last_error,
    };
  }
  if (!backups.enabled) {
    return {
      tone: "warn",
      title: "Recording normally, but backups are off",
      body: "The database is the only copy of your history. Set BACKUP_ENABLED=true.",
    };
  }
  return {
    tone: "ok",
    title: "Recording normally",
    body: `Every ${poller.current_interval_seconds}s via ${poller.last_source ?? "the gateway"}.`,
  };
}

function Verdict({ status, now }) {
  const { tone, title, body } = verdict(status, now);
  const style = TONE[tone];
  return (
    <div
      className={`rounded-2xl border px-5 py-4 ${style.border} ${style.bg}`}
      role={tone === "ok" ? "status" : "alert"}
    >
      <div className="flex items-center gap-2.5">
        <span
          className={`h-2.5 w-2.5 shrink-0 rounded-full ${style.dot} ${
            tone === "ok" ? "live-pulse" : ""
          }`}
          aria-hidden="true"
        />
        <p className={`font-medium ${style.text}`}>{title}</p>
      </div>
      {body && <p className="mt-1.5 pl-5 text-sm text-ink-muted">{body}</p>}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <Panel className="px-5 py-4">
      <h2 className="eyebrow text-ink-faint">{title}</h2>
      <dl className="mt-3 space-y-2">{children}</dl>
    </Panel>
  );
}

function Row({ label, value, tone }) {
  return (
    <div className="flex items-baseline justify-between gap-4 text-sm">
      <dt className="shrink-0 text-ink-muted">{label}</dt>
      <dd
        className={`readout min-w-0 truncate text-right ${
          tone ? TONE[tone].text : "text-ink"
        }`}
        title={typeof value === "string" ? value : undefined}
      >
        {value}
      </dd>
    </div>
  );
}

/* -- Coverage strip ------------------------------------------------------- */

async function fetchLastTwoDays(options) {
  const today = await fetchHistory({ range: "day", date: todayIso() }, options);
  const yesterday = await fetchHistory(
    { range: "day", date: today.previous_date },
    options,
  );
  return [
    { label: "Yesterday", points: yesterday.points },
    { label: "Today", points: today.points },
  ];
}

// An hour that began moments ago has had no chance to be recorded yet.
const CURRENT_HOUR_GRACE_S = 180;

function cellState(point, now, recordingSince, pollSeconds) {
  const start = new Date(point.t).getTime();
  const end = start + 3600 * 1000;
  if (recordingSince != null && end <= recordingSince) return "before";

  // Judge each hour only against the part of it that has passed and that the
  // service existed for, so the first and the current hour are not penalised.
  const from = Math.max(start, recordingSince ?? start);
  const elapsed = (Math.min(end, now) - from) / 1000;
  if (start > now || (now < end && elapsed < CURRENT_HOUR_GRACE_S)) return "future";

  // Coverage only extends to the latest sample, so the running hour always
  // trails real time by up to a poll interval (plus one of slack).
  const expected = now < end ? Math.max(1, elapsed - 2 * pollSeconds) : elapsed;
  const fraction = expected > 0 ? point.covered_seconds / expected : 0;
  if (fraction >= 0.95) return "full";
  if (fraction > 0) return "partial";
  return "none";
}

const CELL_CLASS = {
  full: "bg-grid",
  partial: "bg-solar",
  none: "bg-alert/70",
  future: "bg-hairline/60",
  before: "bg-hairline/30",
};

const CELL_LABEL = {
  full: "fully recorded",
  partial: "partly recorded",
  none: "not recorded",
  future: "not yet",
  before: "before recording began",
};

const HOUR_TICKS = ["00", "06", "12", "18", "24"];

function CoverageStrip({ now, recordingSince, pollSeconds }) {
  const fetcher = useCallback((options) => fetchLastTwoDays(options), []);
  const coverage = usePolledResource(fetcher, { intervalMs: COVERAGE_POLL_MS });

  if (!coverage.data) {
    return (
      <Panel className="px-5 py-4">
        <h2 className="eyebrow text-ink-faint">Recording coverage</h2>
        <p className="mt-3 text-sm text-ink-muted">
          {coverage.error ? "Could not load coverage." : "Loading..."}
        </p>
      </Panel>
    );
  }

  const since = recordingSince ? new Date(recordingSince).getTime() : null;
  const days = coverage.data.map((day) => ({
    ...day,
    cells: day.points.map((point) => ({ point, state: cellState(point, now, since, pollSeconds) })),
  }));
  const counted = days
    .flatMap((day) => day.cells)
    .filter((cell) => cell.state !== "future" && cell.state !== "before");
  const full = counted.filter((cell) => cell.state === "full").length;
  const missing = counted.filter((cell) => cell.state === "none").length;
  const hasBefore = days.some((day) => day.cells.some((cell) => cell.state === "before"));

  return (
    <Panel className="px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="eyebrow text-ink-faint">Recording coverage</h2>
        <p className="text-xs text-ink-muted">
          <span className="readout text-ink">{full}</span> of{" "}
          <span className="readout text-ink">{counted.length}</span> hours complete
          {missing > 0 && (
            <>
              {" "}
              &middot; <span className="readout text-alert">{missing}</span> missing
            </>
          )}
        </p>
      </div>

      <div
        className="mt-3 space-y-1.5"
        role="img"
        aria-label={`${full} of ${counted.length} hours in the last two days fully recorded, ${missing} with no readings.`}
      >
        {days.map((day) => (
          <div key={day.label} className="flex items-center gap-3">
            <span className="w-16 shrink-0 text-xs text-ink-muted">{day.label}</span>
            {/* A DST day has 23 or 25 hours, so the row sizes to its points. */}
            <div
              className="grid flex-1 gap-[3px]"
              style={{ gridTemplateColumns: `repeat(${day.cells.length}, minmax(0, 1fr))` }}
            >
              {day.cells.map(({ point, state }) => (
                <span
                  key={point.t}
                  className={`h-4 rounded-[3px] ${CELL_CLASS[state]}`}
                  title={`${formatDateTime(point.t)}: ${CELL_LABEL[state]}`}
                />
              ))}
            </div>
          </div>
        ))}
        <div className="flex gap-3" aria-hidden="true">
          <span className="w-16 shrink-0" />
          <div className="flex flex-1 justify-between text-[0.6875rem] text-ink-faint">
            {HOUR_TICKS.map((tick) => (
              <span key={tick} className="readout">
                {tick}
              </span>
            ))}
          </div>
        </div>
      </div>

      <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
        {["full", "partial", "none", ...(hasBefore ? ["before"] : [])].map((state) => (
          <li key={state} className="flex items-center gap-1.5">
            <span className={`h-2.5 w-2.5 rounded-sm ${CELL_CLASS[state]}`} aria-hidden="true" />
            {CELL_LABEL[state][0].toUpperCase() + CELL_LABEL[state].slice(1)}
          </li>
        ))}
      </ul>
    </Panel>
  );
}

/* -- View ----------------------------------------------------------------- */

export default function SystemView() {
  const status = usePolledResource(fetchStatus, { intervalMs: STATUS_POLL_MS });
  const now = useNow();

  if (status.loading && !status.data) return <LoadingState label="Checking the service" />;
  if (status.error && !status.data) {
    return <ErrorState error={status.error} onRetry={status.refresh} />;
  }

  const { poller, gateway, storage, tasks, backups, config } = status.data;
  const successRate = poller.poll_count
    ? (poller.success_count / poller.poll_count) * 100
    : null;
  const uptime = (now - new Date(status.data.started_at).getTime()) / 1000;

  return (
    <div className="space-y-4">
      <Verdict status={status.data} now={now} />
      <CoverageStrip
        now={now}
        recordingSince={storage.first_timestamp}
        pollSeconds={poller.current_interval_seconds}
      />

      <div className="grid gap-4 sm:grid-cols-2">
        <Section title="Poller">
          <Row label="Last reading" value={formatRelative(poller.last_success_at, now)} />
          <Row label="Next poll" value={formatRelative(poller.next_poll_at, now)} />
          <Row label="Interval" value={`${poller.current_interval_seconds}s`} />
          <Row
            label="Success rate"
            value={
              successRate == null
                ? "--"
                : `${successRate.toFixed(successRate === 100 ? 0 : 1)}% of ${formatCount(poller.poll_count)}`
            }
            tone={successRate != null && successRate < 95 ? "warn" : undefined}
          />
          {poller.last_error && (
            <Row
              label="Last error"
              value={`${poller.last_error} (${formatRelative(poller.last_failure_at, now)})`}
              tone="alert"
            />
          )}
          {poller.implausible_readings && (
            <Row label="Readings" value="implausible -- check GRID_SCALE" tone="warn" />
          )}
        </Section>

        <Section title="Gateway">
          <Row
            label="Status"
            value={poller.gateway_reachable ? "Reachable" : "Unreachable"}
            tone={poller.gateway_reachable ? "ok" : "alert"}
          />
          <Row label="Address" value={gateway.host ?? "--"} />
          <Row label="Serial" value={gateway.serial_number ?? "--"} />
          <Row label="Read path" value={gateway.last_source ?? "--"} />
          <Row label="Grid CT scale" value={`x ${config.grid_scale}`} />
        </Section>

        <Section title="Storage">
          <Row label="Readings" value={formatCount(storage.reading_count)} />
          <Row label="Recording since" value={formatDateTime(storage.first_timestamp)} />
          <Row label="Database size" value={formatBytes(storage.size_bytes)} />
          <Row label="Rolled up to" value={formatDateTime(storage.latest_rollup_hour)} />
        </Section>

        <Section title="Backups">
          <Row
            label="Daily backup"
            value={backups.enabled ? "On" : "Off"}
            tone={backups.enabled ? "ok" : "warn"}
          />
          <Row label="Latest" value={backups.latest ?? "none yet"} />
          <Row label="Kept" value={`${backups.count} of ${backups.keep_days} days`} />
          <Row label="Last size" value={formatBytes(backups.last_run_size_bytes)} />
        </Section>
      </div>

      <Section title="Background jobs">
        {tasks.map((task) => (
          <Row
            key={task.name}
            label={task.name}
            value={
              task.last_error
                ? `failing: ${task.last_error}`
                : `ran ${formatRelative(task.last_run_at, now)}, every ${formatDuration(task.interval_seconds)}`
            }
            tone={task.last_error ? "alert" : !task.running ? "warn" : undefined}
          />
        ))}
      </Section>

      <footer className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 text-xs text-ink-faint">
        <span>v{status.data.version}</span>
        <span aria-hidden="true">/</span>
        <span>Up {formatDuration(uptime)}</span>
        <span aria-hidden="true">/</span>
        <span>{config.timezone}</span>
        {status.error && (
          <>
            <span aria-hidden="true">/</span>
            <span className="text-alert">refresh failed, retrying</span>
          </>
        )}
      </footer>
    </div>
  );
}
