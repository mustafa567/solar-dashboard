// All requests are same-origin: FastAPI serves this bundle in production, and
// Vite proxies /api to the backend in dev.

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request(path, { signal } = {}) {
  let response;
  try {
    response = await fetch(path, { signal, headers: { Accept: "application/json" } });
  } catch (cause) {
    if (cause.name === "AbortError") throw cause;
    throw new ApiError("Cannot reach the dashboard service.", 0);
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // Non-JSON error body; the status-based message is good enough.
    }
    throw new ApiError(detail, response.status);
  }
  return response.json();
}

export const fetchLive = (options) => request("/api/live", options);

export const fetchHistory = ({ range, date }, options) => {
  const params = new URLSearchParams({ range });
  if (date) params.set("date", date);
  return request(`/api/history?${params}`, options);
};

export const fetchStatus = (options) => request("/api/status", options);

export const exportUrl = (from, to, format = "csv") =>
  `/api/export?${new URLSearchParams({ from, to, format })}`;

export { ApiError };
