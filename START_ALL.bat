@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   ClassTrack - Starting Web Dashboard and Desktop App
echo ============================================================
echo.

:: Kill any existing python server on port 8000 first
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo 1. Launching Web Dashboard...
pushd "%~dp0web-dashboard"
start "ClassTrack Web Dashboard" cmd /k "START_DASHBOARD.bat"
popd

echo    Waiting for Web Dashboard to initialize...
set "DASHBOARD_READY=0"
for /l %%i in (1,1,25) do (
    curl.exe -s -o nul -w "%%{http_code}" http://127.0.0.1:8000/api/edge/status 2>nul | findstr "200" >nul
    if not errorlevel 1 (
        set "DASHBOARD_READY=1"
        goto :dashboard_up
    )
    timeout /t 1 /nobreak >nul
)

:dashboard_up
if "%DASHBOARD_READY%"=="1" (
    echo    Web Dashboard is ONLINE!
) else (
    echo    Web Dashboard still starting, launching Desktop App anyway...
)

echo 2. Launching Desktop App (Camera Node)...
pushd "%~dp0desktop-app"
start "ClassTrack Desktop App" cmd /k "START_DESKTOP.bat"
popd

echo.
echo ============================================================
echo   Both systems launched!
echo   - Web Dashboard: http://127.0.0.1:8000
echo   - Desktop App:   OpenCV Camera Window with HUD overlay
echo ============================================================
echo.
echo Close this window when done.
pause
