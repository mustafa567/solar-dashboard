import { useEffect, useState } from "react";

/**
 * A value that changes when the viewport is resized, debounced.
 *
 * Recharts' ResponsiveContainer re-renders its axes on resize but can keep the
 * previous width for the plotted series, which leaves the data squeezed into
 * part of the frame while the axis spans all of it. Using this as a `key` on
 * the container remounts the chart after a resize settles, so the series and
 * the axis are always measured against the same width.
 */
export function useResizeKey(delayMs = 180) {
  const [key, setKey] = useState(
    () => `${window.innerWidth}x${window.innerHeight}`,
  );

  useEffect(() => {
    let timer;
    const onResize = () => {
      clearTimeout(timer);
      timer = setTimeout(
        () => setKey(`${window.innerWidth}x${window.innerHeight}`),
        delayMs,
      );
    };
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
    };
  }, [delayMs]);

  return key;
}
