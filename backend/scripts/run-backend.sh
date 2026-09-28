#!/usr/bin/env bash
# ===========================================================================
#  Launches the solar dashboard backend (poller + API + rollups + backups).
#
#  POSIX counterpart of run-backend.bat. This one script is what you run by
#  hand for testing and what systemd runs as a service, so there is only one
#  definition of "how the backend starts".
#
#  Resolves paths relative to itself, so it works from any directory.
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

PYTHON="${PROJECT_ROOT}/.venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
    echo "[ERROR] No virtualenv at ${PROJECT_ROOT}/.venv" >&2
    echo "        Create it first:" >&2
    echo "            python3 -m venv .venv" >&2
    echo "            .venv/bin/python -m pip install -r backend/requirements.txt" >&2
    exit 1
fi

if [ ! -f "${PROJECT_ROOT}/.env" ]; then
    echo "[WARN] No .env in ${PROJECT_ROOT} -- copy .env.example and set PVS_HOST / PVS_SN." >&2
fi

# Read API_HOST / API_PORT from .env so the service and the config agree.
API_HOST="0.0.0.0"
API_PORT="8000"
if [ -f "${PROJECT_ROOT}/.env" ]; then
    while IFS='=' read -r key value; do
        case "${key}" in
            API_HOST) API_HOST="${value%%#*}" ;;
            API_PORT) API_PORT="${value%%#*}" ;;
        esac
    done < <(grep -E '^(API_HOST|API_PORT)=' "${PROJECT_ROOT}/.env" || true)
    API_HOST="$(echo "${API_HOST}" | tr -d '[:space:]')"
    API_PORT="$(echo "${API_PORT}" | tr -d '[:space:]')"
fi

cd "${PROJECT_ROOT}"

echo "Starting solar dashboard on ${API_HOST}:${API_PORT}"
exec "${PYTHON}" -m uvicorn app.main:app \
    --app-dir backend \
    --host "${API_HOST}" \
    --port "${API_PORT}" \
    --no-access-log
