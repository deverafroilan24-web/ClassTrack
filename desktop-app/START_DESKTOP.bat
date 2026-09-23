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
        echo [NOTICE] Existing .venv has broken paths [likely transferred from another PC].
        echo Recreating clean virtual environment...
        rmdir /s /q .venv >nul 2>&1
    )
)

if not exist "%PYTHON%" (
    echo Creating local Python environment for Desktop App...
    python -m venv .venv 2>nul || py -3 -m venv .venv
    if errorlevel 1 (
        echo ============================================================
        echo [ERROR] Python was not found on your computer!
        echo.
        echo To run ClassTrack from source, install Python 3.10 or 3.11:
        echo 1. Download: https://www.python.org/downloads/
        echo 2. IMPORTANT: Check "Add python.exe to PATH" during setup!
        echo.
        echo NOTE FOR NON-TECH USERS:
        echo You do NOT need Python if you run the standalone installer:
        echo Double-click: ClassTrack_Setup_v1.0.exe
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

"%PYTHON%" -c "import cv2, ultralytics, torch, websocket" >nul 2>nul
if errorlevel 1 (
    echo Installing Python packages...
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed. Read the error above, then run START_DESKTOP.bat again.
        pause
        exit /b 1
    )
)

set "CAM_ARG="
if not "%~1"=="" (
    set "CAM_ARG=--cam %~1"
)

echo Starting Desktop App (Camera Node)...
"%PYTHON%" launch_desktop.py %CAM_ARG%
