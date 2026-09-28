@echo off
REM ===========================================================================
REM  Fallback to NSSM: register the backend as a Scheduled Task that runs at
REM  logon.
REM
REM  Simpler (no extra software), but weaker than a service:
REM    - it starts at LOGON, not at boot, so the PC must be logged in
REM    - logging out stops the poller, which stops history accumulating
REM  Prefer install-service-nssm.bat unless you specifically want this.
REM ===========================================================================

setlocal

set "TASK_NAME=SolarDashboard"
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "PROJECT_ROOT=%%~fI"
set "LAUNCHER=%PROJECT_ROOT%\backend\scripts\run-backend.bat"

echo Registering scheduled task "%TASK_NAME%" to run at logon...
schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "\"%LAUNCHER%\"" ^
    /SC ONLOGON ^
    /RL HIGHEST ^
    /F
if errorlevel 1 (
    echo [ERROR] schtasks failed. Try running this from an elevated prompt.
    exit /b 1
)

REM Keep it running indefinitely rather than being killed after 72 hours.
schtasks /Change /TN "%TASK_NAME%" /ET 00:00 >nul 2>&1

echo.
echo Done. Useful commands:
echo     schtasks /Query  /TN "%TASK_NAME%" /V /FO LIST
echo     schtasks /Run    /TN "%TASK_NAME%"
echo     schtasks /End    /TN "%TASK_NAME%"
echo     schtasks /Delete /TN "%TASK_NAME%" /F
echo.
echo Start it now with:  schtasks /Run /TN "%TASK_NAME%"

endlocal
