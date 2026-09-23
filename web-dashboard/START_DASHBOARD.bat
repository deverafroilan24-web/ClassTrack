@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Creating local Python environment for Web Dashboard...
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
