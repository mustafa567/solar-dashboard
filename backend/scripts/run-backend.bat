@echo off
REM ===========================================================================
REM  Launches the solar dashboard backend (poller + API + rollups + backups).
REM
REM  This one script is what you run by hand for testing, what NSSM runs as a
REM  service, and what Task Scheduler runs at startup -- so there is only one
REM  definition of "how the backend starts".
REM
REM  It resolves paths relative to itself, so it works from any directory.
REM ===========================================================================

setlocal

REM scripts\ -> backend\ -> project root
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "PROJECT_ROOT=%%~fI"

set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] No virtualenv found at %PROJECT_ROOT%\.venv
    echo         Create it first:
    echo             python -m venv .venv
    echo             .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
    exit /b 1
)

if not exist "%PROJECT_ROOT%\.env" (
    echo [WARN] No .env in %PROJECT_ROOT% -- copy .env.example to .env and set PVS_HOST / PVS_SN.
)

REM Read API_HOST / API_PORT out of .env so the service and the config agree.
set "API_HOST=0.0.0.0"
set "API_PORT=8000"
if exist "%PROJECT_ROOT%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%PROJECT_ROOT%\.env") do (
        if /i "%%A"=="API_HOST" set "API_HOST=%%B"
        if /i "%%A"=="API_PORT" set "API_PORT=%%B"
    )
)

cd /d "%PROJECT_ROOT%"

echo Starting solar dashboard on %API_HOST%:%API_PORT%
"%PYTHON%" -m uvicorn app.main:app --app-dir backend --host %API_HOST% --port %API_PORT% --no-access-log

endlocal
