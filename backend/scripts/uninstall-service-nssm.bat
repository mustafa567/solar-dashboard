@echo off
REM Removes the SolarDashboard service. Run from an ELEVATED prompt.
REM The database in data\ is left untouched.

setlocal
set "SERVICE_NAME=SolarDashboard"
if "%NSSM%"=="" set "NSSM=nssm.exe"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Run this from an elevated Command Prompt ^(Run as administrator^).
    exit /b 1
)

%NSSM% stop "%SERVICE_NAME%"
%NSSM% remove "%SERVICE_NAME%" confirm
echo Service removed. data\solar.db was NOT deleted.
endlocal
