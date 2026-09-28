@echo off
REM Builds the React frontend into frontend\dist, which FastAPI serves at /.
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%\frontend" || exit /b 1
call npm install || exit /b 1
call npm run build || exit /b 1
echo.
echo Built to %PROJECT_ROOT%\frontend\dist
endlocal
