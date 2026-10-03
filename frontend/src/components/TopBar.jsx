const TABS = [
  { id: "now", label: "Now" },
  { id: "analyze", label: "Analyze" },
  { id: "system", label: "System" },
];

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
      <circle cx="12" cy="12" r="4.2" fill="currentColor" />
      {[0, 45, 90, 135, 180, 225, 270, 315].map((angle) => (
        <line
          key={angle}
          x1="12"
          y1="4"
          x2="12"
          y2="1.8"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          transform={`rotate(${angle} 12 12)`}
        />
      ))}
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
      <path
        d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"
        fill="currentColor"
      />
    </svg>
  );
}

const MODE_LABEL = {
  auto: "Following the sun",
  day: "Daytime colours",
  night: "Night colours",
};

function ThemeToggle({ theme, mode, onCycle }) {
  return (
    <button
      type="button"
      onClick={onCycle}
      title={`${MODE_LABEL[mode]} - tap to change`}
      aria-label={`Theme: ${MODE_LABEL[mode]}. Tap to change.`}
      className="relative rounded-full border border-hairline bg-panel p-2 text-ink-muted transition-colors hover:bg-panel-raised hover:text-ink"
    >
      {theme === "day" ? <SunIcon /> : <MoonIcon />}
      {mode === "auto" && (
        // A dot marks "following the sun" rather than a manual choice.
        <span
          className="absolute -bottom-0.5 left-1/2 h-1 w-1 -translate-x-1/2 rounded-full bg-grid"
          aria-hidden="true"
        />
      )}
    </button>
  );
}

export default function TopBar({
  view,
  onViewChange,
  connected,
  hasData,
  theme,
  themeMode,
  onCycleTheme,
}) {
  const dotClass = !hasData
    ? "bg-ink-faint"
    : connected
      ? "bg-grid live-pulse"
      : "bg-alert";
  const dotLabel = !hasData
    ? "Waiting for data"
    : connected
      ? "Gateway connected"
      : "Gateway unreachable";

  return (
    <header className="sticky top-0 z-20 border-b border-hairline bg-void/70 backdrop-blur-md">
      <div className="mx-auto flex max-w-3xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
        {/* The dot is the at-a-glance health light; it opens the System view. */}
        <button
          type="button"
          onClick={() => onViewChange("system")}
          className="flex items-center gap-2.5 rounded-lg py-1 pr-1"
          title={`${dotLabel} - open System`}
        >
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${dotClass}`}
            aria-hidden="true"
          />
          <span className="eyebrow text-ink">Solar</span>
          <span className="sr-only">{dotLabel}. Open system status.</span>
        </button>

        <div className="flex items-center gap-2">
          <nav
            className="flex rounded-full border border-hairline bg-panel p-0.5"
            aria-label="Views"
          >
            {TABS.map((tab) => {
              const active = view === tab.id;
              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => onViewChange(tab.id)}
                  aria-current={active ? "page" : undefined}
                  className={`rounded-full px-3 py-1.5 text-sm font-medium sm:px-4 transition-colors ${
                    active
                      ? "bg-panel-raised text-ink"
                      : "text-ink-muted hover:text-ink"
                  }`}
                >
                  {tab.label}
                </button>
              );
            })}
          </nav>
          <ThemeToggle theme={theme} mode={themeMode} onCycle={onCycleTheme} />
        </div>
      </div>
    </header>
  );
}
