// Formatting helpers. Power and energy are shown at the precision a homeowner
// can act on: two decimals for kW, one for kWh.

export const formatKw = (value) =>
  value == null || Number.isNaN(value) ? "--" : value.toFixed(2);

export const formatKwh = (value) =>
  value == null || Number.isNaN(value) ? "--" : value.toFixed(1);

export const formatPct = (value) =>
  value == null || Number.isNaN(value) ? "--" : `${value.toFixed(0)}%`;

export const todayIso = () => {
  const now = new Date();
  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
};

export const formatClock = (isoString) => {
  if (!isoString) return null;
  const parsed = new Date(isoString);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
};

export const formatAge = (seconds) => {
  if (seconds == null) return "no readings yet";
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
};

// Direction words, used for the grid card's supporting line.
export const gridPhrase = (direction) => {
  if (direction === "import") return "Drawing from the grid";
  if (direction === "export") return "Sending to the grid";
  if (direction === "idle") return "Nothing flowing";
  return "No reading";
};
