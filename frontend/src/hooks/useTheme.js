import { useCallback, useEffect, useMemo, useState } from "react";

const STORAGE_KEY = "solar.theme";
const MODES = ["auto", "day", "night"];

/** Daylight hours, used only when the gateway cannot tell us about the sun. */
const DAY_START_HOUR = 7;
const DAY_END_HOUR = 19;

/**
 * Day/night theming driven by the array itself.
 *
 * The honest signal for "is it daytime" on a solar dashboard is whether the
 * panels are producing, so that is what drives it. The clock is only a fallback
 * for when there is no reading yet. `mode` cycles auto -> day -> night and is
 * remembered per browser.
 */
export function useTheme(live) {
  const [mode, setMode] = useState(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      return MODES.includes(stored) ? stored : "auto";
    } catch {
      // Private window or blocked storage: auto is a fine default.
      return "auto";
    }
  });

  const autoTheme = useMemo(() => {
    if (live?.has_data && typeof live.solar_kw === "number") {
      // A trickle at dawn already counts: the page brightens as the array wakes.
      return live.solar_kw > 0.05 ? "day" : "night";
    }
    const hour = new Date().getHours();
    return hour >= DAY_START_HOUR && hour < DAY_END_HOUR ? "day" : "night";
  }, [live?.has_data, live?.solar_kw]);

  const theme = mode === "auto" ? autoTheme : mode;

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    // Mobile browser chrome follows the page, not the colour baked into
    // index.html at build time.
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      const colour = getComputedStyle(document.documentElement)
        .getPropertyValue("--color-void")
        .trim();
      if (colour) meta.setAttribute("content", colour);
    }
  }, [theme]);

  const cycleMode = useCallback(() => {
    setMode((current) => {
      const next = MODES[(MODES.indexOf(current) + 1) % MODES.length];
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // Not being able to remember the choice is not worth failing over.
      }
      return next;
    });
  }, []);

  return { theme, mode, cycleMode };
}
