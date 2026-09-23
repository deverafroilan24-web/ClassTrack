@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Creating local Python environment for Desktop App...
    python -m venv .venv 2>nul || py -3 -m venv .venv
    if errorlevel 1 (
        echo Unable to create .venv. Install Python 3 and make sure python or the py launcher is available.
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
