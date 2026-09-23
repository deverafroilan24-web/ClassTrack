@echo off
setlocal
cd /d "%~dp0"

:: Check if running from inside an unextracted zip
echo "%~dp0" | findstr /i "Temp" >nul
if not errorlevel 1 (
    echo ============================================================
    echo [ERROR] You are running directly from inside a ZIP file!
    echo Please EXTRACT all files into a regular folder first.
    echo ============================================================
    pause
    exit /b 1
)

set "PYTHON=%CD%\.venv\Scripts\python.exe"

:: If .venv exists, verify it actually works (handles copied environments from other PCs)
if exist "%PYTHON%" (
    "%PYTHON%" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo [NOTICE] Existing .venv has broken paths (likely transferred from another PC).
        echo Recreating clean virtual environment...
        rmdir /s /q .venv >nul 2>&1
    )
)

if not exist "%PYTHON%" (
    echo Creating local Python environment for Web Dashboard...
    python -m venv .venv 2>nul || py -3 -m venv .venv
    if errorlevel 1 (
        echo ============================================================
        echo [ERROR] Python was not found on your computer!
        echo.
        echo To run the local Web Dashboard, install Python 3.10+:
        echo 1. Download: https://www.python.org/downloads/
        echo 2. IMPORTANT: Check "Add python.exe to PATH" during setup!
        echo.
        echo NOTE: The Web Dashboard is ALREADY LIVE on the cloud!
        echo You can simply open: https://classtrack-mm41.onrender.com
        echo ============================================================
        pause
        exit /b 1
    )
)

if not exist ".env" (
    if exist ".env.example" (
        echo Creating .env from .env.example...
        copy ".env.example" ".env" >nul
    )
)

"%PYTHON%" -c "import fastapi, uvicorn, pydantic" >nul 2>nul
if errorlevel 1 (
    echo Installing Python packages...
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed. Read the error above, then run START_DASHBOARD.bat again.
        pause
        exit /b 1
    )
)

echo.
echo ============================================================
echo   ClassTrack Web Dashboard
echo   URL:   http://127.0.0.1:8000
echo   Docs:  http://127.0.0.1:8000/docs
echo ============================================================
echo.

start "" "http://127.0.0.1:8000"
"%PYTHON%" run_server.py --host 127.0.0.1 --port 8000 --no-reload
