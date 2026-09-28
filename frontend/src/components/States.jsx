// Empty, error and loading states. Each one says what happened and what to do
// next, rather than showing a bare spinner or an apology.

export function Panel({ children, className = "" }) {
  return (
    <div
      className={`panel-surface rounded-2xl border border-hairline bg-panel/70 ${className}`}
    >
      {children}
    </div>
  );
}

export function EmptyState({ title, body, action }) {
  return (
    <Panel className="px-6 py-12 text-center">
      <p className="text-ink font-medium">{title}</p>
      {body && (
        <p className="mt-2 text-sm text-ink-muted max-w-sm mx-auto leading-relaxed">
          {body}
        </p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </Panel>
  );
}

export function ErrorState({ error, onRetry }) {
  return (
    <Panel className="px-6 py-10 text-center border-alert/40">
      <p className="eyebrow text-alert">Cannot load</p>
      <p className="mt-3 text-ink">{error?.message ?? "Something went wrong."}</p>
      <p className="mt-2 text-sm text-ink-muted">
        Check that the dashboard service is running on this PC.
      </p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-5 rounded-lg border border-hairline px-4 py-2 text-sm font-medium text-ink hover:bg-panel-raised transition-colors"
        >
          Try again
        </button>
      )}
    </Panel>
  );
}

export function LoadingState({ label = "Loading" }) {
  return (
    <Panel className="px-6 py-12 text-center">
      <div className="flex items-center justify-center gap-2 text-ink-muted">
        <span className="live-pulse h-2 w-2 rounded-full bg-grid" />
        <span className="text-sm">{label}</span>
      </div>
    </Panel>
  );
}

export function Skeleton({ className = "" }) {
  return (
    <div
      className={`live-pulse rounded-lg bg-panel-raised ${className}`}
      aria-hidden="true"
    />
  );
}
