@echo off
REM ===========================================================================
REM  Installs the backend as a Windows service using NSSM.
REM
REM  Run this from an ELEVATED (Administrator) Command Prompt.
REM  NSSM must be on PATH, or set NSSM=C:\path\to\nssm.exe before running.
REM
REM  A service (unlike Task Scheduler) keeps running when you log out, starts
REM  before login at boot, and restarts itself if the process dies -- which is
REM  what you want, because a stopped poller is a permanent hole in the history.
REM ===========================================================================

setlocal

set "SERVICE_NAME=SolarDashboard"
if "%NSSM%"=="" set "NSSM=nssm.exe"

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "PROJECT_ROOT=%%~fI"
set "LAUNCHER=%PROJECT_ROOT%\backend\scripts\run-backend.bat"
set "LOG_DIR=%PROJECT_ROOT%\logs"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Run this from an elevated Command Prompt ^(Run as administrator^).
    exit /b 1
)

where %NSSM% >nul 2>&1
if errorlevel 1 (
    echo [ERROR] nssm.exe not found on PATH.
    echo         Download it from https://nssm.cc/download, or set NSSM first:
    echo             set NSSM=C:\tools\nssm\win64\nssm.exe
    exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo Installing service "%SERVICE_NAME%"...
%NSSM% install "%SERVICE_NAME%" "%LAUNCHER%"
if errorlevel 1 (
    echo [ERROR] nssm install failed. If the service already exists, run
    echo         uninstall-service-nssm.bat first.
    exit /b 1
)

REM --- Identity and working directory ---------------------------------------
%NSSM% set "%SERVICE_NAME%" AppDirectory "%PROJECT_ROOT%"
%NSSM% set "%SERVICE_NAME%" DisplayName "Solar Dashboard (PVS6 poller and API)"
%NSSM% set "%SERVICE_NAME%" Description "Polls the SunPower PVS6 gateway, stores readings in SQLite and serves the dashboard."
%NSSM% set "%SERVICE_NAME%" Start SERVICE_AUTO_START

REM --- Restart policy: always come back, but do not spin on a hard failure ---
%NSSM% set "%SERVICE_NAME%" AppExit Default Restart
%NSSM% set "%SERVICE_NAME%" AppRestartDelay 10000
%NSSM% set "%SERVICE_NAME%" AppThrottle 10000

REM --- Logging: rotate so logs cannot fill the disk over months of uptime ----
%NSSM% set "%SERVICE_NAME%" AppStdout "%LOG_DIR%\service.out.log"
%NSSM% set "%SERVICE_NAME%" AppStderr "%LOG_DIR%\service.err.log"
%NSSM% set "%SERVICE_NAME%" AppRotateFiles 1
%NSSM% set "%SERVICE_NAME%" AppRotateOnline 1
%NSSM% set "%SERVICE_NAME%" AppRotateSeconds 86400
%NSSM% set "%SERVICE_NAME%" AppRotateBytes 10485760

REM --- Shutdown: let the poller finish its current write and close the DB ----
%NSSM% set "%SERVICE_NAME%" AppStopMethodConsole 15000
%NSSM% set "%SERVICE_NAME%" AppStopMethodWindow 5000
%NSSM% set "%SERVICE_NAME%" AppStopMethodThreads 5000

REM --- Wait for the network before the first poll ----------------------------
%NSSM% set "%SERVICE_NAME%" DependOnService Tcpip Dnscache

echo.
echo Starting "%SERVICE_NAME%"...
%NSSM% start "%SERVICE_NAME%"

echo.
echo Done. Useful commands:
echo     %NSSM% status  %SERVICE_NAME%
echo     %NSSM% restart %SERVICE_NAME%
echo     %NSSM% stop    %SERVICE_NAME%
echo     %NSSM% edit    %SERVICE_NAME%
echo.
echo Logs:  %LOG_DIR%\service.out.log  and  service.err.log
echo Health check:  curl http://localhost:8000/api/health

endlocal
