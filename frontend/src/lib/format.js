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

export const formatBytes = (bytes) => {
  if (bytes == null) return "--";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
};

export const formatCount = (value) =>
  value == null ? "--" : Number(value).toLocaleString();

/** "Mon 28 Sep, 15:04" in the browser's locale. */
export const formatDateTime = (isoString) => {
  if (!isoString) return "--";
  const parsed = new Date(isoString);
  if (Number.isNaN(parsed.getTime())) return "--";
  return parsed.toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
};

/** A span of seconds as the two largest units: "3 d 4 h", "12 min". */
export const formatDuration = (seconds) => {
  if (seconds == null || Number.isNaN(seconds)) return "--";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s}s`;
  const minutes = Math.floor(s / 60);
  if (minutes < 90) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${minutes % 60} min`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
};

/** Relative to now, in either direction: "4 min ago", "in 40s". */
export const formatRelative = (isoString, now = Date.now()) => {
  if (!isoString) return "never";
  const parsed = new Date(isoString).getTime();
  if (Number.isNaN(parsed)) return "--";
  const delta = (parsed - now) / 1000;
  if (Math.abs(delta) < 5) return "just now";
  return delta > 0 ? `in ${formatDuration(delta)}` : `${formatDuration(-delta)} ago`;
};
