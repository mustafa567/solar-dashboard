import { useCallback, useEffect, useRef, useState } from "react";
import { todayIso } from "../lib/format.js";

/*
  Hash routing, so a refresh keeps you where you were and a period can be
  bookmarked: #/now, #/analyze/month/2026-03-01, #/system.

  A hash rather than the History API because FastAPI's SPA fallback would work
  either way, but the Vite dev server and a file:// preview only work with a
  hash -- and there is nothing here worth a router dependency.
*/

export const VIEWS = ["now", "analyze", "system"];
export const RANGES = ["day", "month", "year"];

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function parseHash(hash) {
  const [view, range, date] = hash.replace(/^#\/?/, "").split("/");

  if (view === "analyze") {
    return {
      view,
      range: RANGES.includes(range) ? range : "day",
      date: ISO_DATE.test(date ?? "") ? date : todayIso(),
    };
  }
  return { view: VIEWS.includes(view) ? view : "now", range: "day", date: todayIso() };
}

export function formatHash({ view, range, date }) {
  if (view === "analyze") return `#/analyze/${range}/${date}`;
  return `#/${view}`;
}

export function useRoute() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));

  // The last Analyze selection survives a trip to another view, so Now ->
  // Analyze returns to the month you were reading rather than resetting.
  const routeRef = useRef(route);
  routeRef.current = route;

  useEffect(() => {
    const onHashChange = () => {
      const parsed = parseHash(window.location.hash);
      setRoute((current) =>
        parsed.view === "analyze"
          ? parsed
          : { ...parsed, range: current.range, date: current.date },
      );
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  // Each navigation is a history entry, so the browser's back button steps
  // back through periods as well as views. State follows via hashchange.
  const navigate = useCallback((next) => {
    const merged = { ...routeRef.current, ...next };
    const hash = formatHash(merged);
    if (hash === window.location.hash) return;
    window.location.hash = hash;
  }, []);

  return [route, navigate];
}
